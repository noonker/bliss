import base64
import json
import os
import shutil
import stat
import subprocess
import tarfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from bliss.agents import BUILTIN, Release
from bliss.runtime import _credentials_script, _native_libs_script, prepare_agent_script, run_script
from bliss.system import AgentSpec, Pack, guix_manifest, nix_shell_roots, write_docker_archive
from bliss.targets.base import Workload
from bliss.targets.image import ensure_system_image, unused_system_images
from bliss.targets.kubernetes import k8s_name, manifests, pull_image
from bliss.targets.ssh import GC_SCRIPT, SshTarget, tmux_logs_command
from bliss.util import BlissError

# A full path, as bliss needs; /bin/bash is missing in a Guix build or on NixOS.
BASH = shutil.which("bash") or "/bin/bash"

SPEC = AgentSpec(BUILTIN["claude"], Release("1.0.0", "https://x/claude", "ab" * 32))
GUIX = Pack(format="docker-archive", key="k", uses_profile=True, bin_dirs=("$BLISS_PROFILE/bin",),
            ca_file="$BLISS_PROFILE/etc/ca.crt", loader="$BLISS_PROFILE/lib/ld.so",
            patchelf="$BLISS_PROFILE/bin/patchelf", agent_root="$BLISS_PROFILE")


class KubernetesTest(unittest.TestCase):
    def test_pod_secret_holds_scripts(self):
        secret, pod = manifests("bliss-x", "reg/i:1", {"env.sh": "export A=1\n"}, {})
        self.assertEqual(pod["kind"], "Pod")
        self.assertEqual(pod["spec"]["restartPolicy"], "Never")
        self.assertEqual(base64.b64decode(secret["data"]["env.sh"]), b"export A=1\n")

    def test_env_never_in_workload_object(self):
        _, pod = manifests("n", "i", {"env.sh": "export TOKEN=hunter2\n"}, {})
        self.assertNotIn("hunter2", json.dumps(pod))
        self.assertNotIn(base64.b64encode(b"export TOKEN=hunter2\n").decode(), json.dumps(pod))
        self.assertEqual(pod["spec"]["volumes"][0]["secret"]["defaultMode"], 0o400)

    def test_deployment(self):
        _, deploy = manifests("n", "i", {}, {"kind": "deployment", "namespace": "ns"})
        self.assertEqual(deploy["kind"], "Deployment")
        self.assertEqual(deploy["metadata"]["namespace"], "ns")
        self.assertEqual(deploy["spec"]["selector"]["matchLabels"]["bliss/workload"], "n")

    def test_pull_registry_only_changes_the_address(self):
        image = "reg.example/me/bliss-work:x"
        self.assertEqual(pull_image(image, "reg.example/me", {}), image)
        self.assertEqual(pull_image(image, "reg.example/me/", {"pull_registry": "10.0.0.5:5000/"}),
                         "10.0.0.5:5000/bliss-work:x")

    def test_name_is_valid(self):
        name = k8s_name("My_Project.Name-" + "x" * 80)
        self.assertLessEqual(len(name), 63)
        self.assertRegex(name, r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


class ManifestTest(unittest.TestCase):
    def test_uses_exact_profile_and_pins_agent(self):
        text = guix_manifest("/gnu/store/p-profile", SPEC)
        self.assertIn('(profile-manifest "/gnu/store/p-profile")', text)
        self.assertIn('(base16-string->bytevector "' + "ab" * 32 + '")', text)
        self.assertIn('"glibc"', text)

    def test_no_agent_means_no_loader(self):
        self.assertNotIn('"glibc"', guix_manifest("/gnu/store/p", None))


class ScriptTest(unittest.TestCase):
    def test_binary_is_only_patched_without_loader(self):
        script = prepare_agent_script(SPEC, GUIX, "$HOME/c")
        self.assertIn("if [ ! -e /lib64/ld-linux-x86-64.so.2 ]", script)
        self.assertIn("--set-interpreter", script)
        self.assertNotIn("--set-rpath", script)

    def test_patches_the_release_not_the_relocation_wrapper(self):
        script = prepare_agent_script(SPEC, GUIX, "$HOME/c")
        self.assertIn("for release in /gnu/store/*-bliss-agent-claude-1.0.0; do break; done", script)
        self.assertIn('cp -L "$release/bin/claude" "$tmp/claude" || bliss_fail $?', script)
        self.assertNotIn('cp -L "$BLISS_PROFILE', script)
        tarball = prepare_agent_script(AgentSpec(BUILTIN["pi"], Release("v1.5.0", "u", "s")), GUIX, "$HOME/c")
        self.assertIn("/gnu/store/*-bliss-agent-pi-1.5.0; do break", tarball)
        self.assertIn('cp -rL "$release/libexec/pi"/.', tarball)

    def test_run_scripts_parse(self):
        for pack, spec in ((GUIX, None), (GUIX, SPEC),
                           (GUIX, AgentSpec(BUILTIN["hermes"], Release("stable", "u", ""))),
                           (GUIX, AgentSpec(BUILTIN["autolith"], Release("v1", "u", "s"))),
                           (GUIX, AgentSpec(BUILTIN["pi"], Release("v1", "u", "s")))):
            for keep in (False, True):
                script = run_script(pack, ["echo", "a b", "$c"], spec=spec,
                                    cache_prefix="$HOME/c", keep_session=keep)
                subprocess.run(["bash", "-n"], input=script.encode(), check=True)

    def test_installer_cannot_touch_real_home(self):
        script = prepare_agent_script(AgentSpec(BUILTIN["hermes"], Release("stable", "u", "")),
                                      GUIX, "$HOME/c")
        self.assertIn('HOME="$cache/.home"', script)


    def _path_after(self, env_sh):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "env.sh").write_text(env_sh)
            pack = Pack(format="relocatable", key="k", bin_dirs=(f"{tmp}/pack/bin",),
                        ca_file="c", bash=BASH)
            script = run_script(pack, ["printenv", "PATH"], spec=None, cache_prefix="$HOME/c")
            return subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True, check=True,
                env={"BLISS_DIR": tmp, "BLISS_WORK": tmp, "HOME": tmp, "PATH": os.environ["PATH"]},
            ).stdout.strip(), f"{tmp}/pack/bin"

    def test_pack_bin_is_on_path_once(self):
        path, pack_bin = self._path_after("")
        self.assertEqual(path.split(":").count(pack_bin), 1)

    def test_pack_bin_stays_reachable_when_env_sets_path(self):
        path, pack_bin = self._path_after(f"export PATH={os.environ['PATH']}\n")
        self.assertEqual(path, f"{os.environ['PATH']}:{pack_bin}")


    def test_tarball_on_nix_is_unpacked_and_found(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            release = root / "pi"
            release.mkdir()
            (release / "pi").write_text(f"#!{BASH}\necho ran\n")
            (release / "pi").chmod(0o755)
            archive = root / "pi.tar.gz"
            subprocess.run(["tar", "czf", archive, "-C", tmp, "pi"], check=True)
            (root / "env.sh").write_text("")
            pack = Pack(format="nix-closure", key="k", agent_root=str(archive),
                        bin_dirs=(), ca_file="c", patchelf="true", loader="l", bash=BASH)
            spec = AgentSpec(BUILTIN["pi"], Release("v1", "u", "s"))
            script = run_script(pack, ["pi"], spec=spec, cache_prefix=f"{tmp}/cache")
            env = {"BLISS_DIR": tmp, "BLISS_WORK": tmp, "HOME": tmp, "PATH": os.environ["PATH"]}
            for _ in range(2):  # the second run reuses the unpacked copy
                result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                        check=True, env=env)
                self.assertEqual(result.stdout, "ran\n")

    def _install_with_fake_curl(self, tmp, curl_body):
        root = Path(tmp)
        fake = root / "fake"
        fake.mkdir()
        (fake / "curl").write_text(f"#!{BASH}\n" + curl_body)
        (fake / "curl").chmod(0o755)
        (root / "env.sh").write_text("")
        pack = Pack(format="relocatable", key="k", bin_dirs=(), ca_file="c", bash=BASH)
        spec = AgentSpec(BUILTIN["hermes"], Release("stable", "u", ""))
        script = run_script(pack, ["true"], spec=spec, cache_prefix=f"{tmp}/cache", keep_session=True)
        env = {"BLISS_DIR": tmp, "BLISS_WORK": tmp, "HOME": tmp,
               "PATH": f"{fake}:{os.environ['PATH']}"}
        return subprocess.run(["bash", "-c", script], stdin=subprocess.DEVNULL, env=env,
                              capture_output=True, text=True)

    def test_failed_download_is_not_recorded_as_installed(self):
        with TemporaryDirectory() as tmp:
            result = self._install_with_fake_curl(tmp, "exit 7\n")
            self.assertIn("setup failed with status 7", result.stdout)
            self.assertEqual((Path(tmp) / "exit").read_text().strip(), "7")
            self.assertFalse((Path(tmp) / "cache/hermes-stable/.ok").exists())

    def test_failed_installer_keeps_the_session_and_tar_cannot_chown(self):
        with TemporaryDirectory() as tmp:
            # The fake curl "downloads" an installer that reports TAR_OPTIONS and fails.
            body = ('while [ "$1" != "-o" ]; do shift; done\n'
                    "printf 'echo \"tar=$TAR_OPTIONS\"\\nexit 3\\n' > \"$2\"\n")
            result = self._install_with_fake_curl(tmp, body)
            self.assertIn("tar=--no-same-owner", result.stdout)
            self.assertIn("setup failed with status 3", result.stdout)
            self.assertFalse((Path(tmp) / "cache/hermes-stable/.ok").exists())

    def test_native_libs_exclude_the_profile_glibc(self):
        with TemporaryDirectory() as tmp:
            lib = Path(tmp) / "profile/lib"
            lib.mkdir(parents=True)
            (lib / "libcrypto.so.3").write_text("")
            (lib / "libc.so.6").write_text("")
            script = _native_libs_script(BUILTIN["autolith"])
            subprocess.run(["bash", "-c", script], check=True, capture_output=True,
                           env={"BLISS_PROFILE": f"{tmp}/profile", "HOME": tmp, "PATH": os.environ["PATH"]})
            found = sorted(p.name for p in (Path(tmp) / ".bliss/lib/autolith").iterdir())
            self.assertEqual(found, ["libcrypto.so.3"])

    def test_guix_links_the_release_executable(self):
        pi = guix_manifest(None, AgentSpec(BUILTIN["pi"], Release("v1", "u", "ab" * 32)))
        self.assertIn('"../libexec/pi/pi"', pi)
        autolith = guix_manifest(None, AgentSpec(BUILTIN["autolith"], Release("v1", "u", "ab" * 32)))
        self.assertIn('"../libexec/autolith/bin/autolith"', autolith)


class UnattendedTest(unittest.TestCase):
    def script(self, *, unattended, sandboxed):
        workload = Workload(id="w", root=Path("."), command=["claude"], env={}, spec=SPEC,
                            unattended=unattended)
        return workload.scripts(GUIX, cache_prefix="$HOME/c", sandboxed=sandboxed)["run.sh"]

    def test_only_unattended_runs_in_a_sandbox_are_marked(self):
        self.assertIn("export IS_SANDBOX=1", self.script(unattended=True, sandboxed=True))
        self.assertNotIn("IS_SANDBOX", self.script(unattended=True, sandboxed=False))
        self.assertNotIn("IS_SANDBOX", self.script(unattended=False, sandboxed=True))


class InstallMemoryTest(unittest.TestCase):
    SPEC = AgentSpec(BUILTIN["hermes"], Release("stable", "u", ""))

    def check(self, reply, spec=None):
        target = SshTarget("c", {"host": "h"})
        with mock.patch("bliss.targets.ssh.output", return_value=reply):
            target._check_install_memory(spec or self.SPEC)

    def test_small_host_is_refused(self):
        with self.assertRaisesRegex(BlissError, "949 MB"):
            self.check("949")

    def test_large_host_or_finished_install_is_allowed(self):
        self.check("8192")
        self.check("0")  # the script reports 0 once the agent is installed

    def test_agents_that_install_nothing_are_not_checked(self):
        target = SshTarget("c", {"host": "h"})
        with mock.patch("bliss.targets.ssh.output", side_effect=AssertionError):
            target._check_install_memory(SPEC)


class CredentialsTest(unittest.TestCase):
    def workload(self, credentials):
        return Workload(id="w", root=Path("."), command=["claude"], env={}, spec=SPEC,
                        credentials=credentials)

    def test_nothing_changes_without_opting_in(self):
        scripts = self.workload({}).scripts(GUIX, cache_prefix="$HOME/c")
        self.assertEqual(set(scripts), {"env.sh", "run.sh"})
        self.assertNotIn("CLAUDE_CONFIG_DIR", scripts["run.sh"])

    def test_copy_is_private_and_existing_login_is_untouched(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            home_login = root / ".claude/.credentials.json"
            home_login.parent.mkdir()
            home_login.write_text("the remote user's own")
            scripts = self.workload({".credentials.json": "sent"}).scripts(
                GUIX, cache_prefix="$HOME/c", credentials_dir="$BLISS_DIR/agent-config")
            for name, text in scripts.items():
                if name.startswith("credential-"):
                    (root / name).write_text(text)
            script = _credentials_script(BUILTIN["claude"], "$BLISS_DIR/agent-config")
            result = subprocess.run(
                ["bash", "-c", script + 'echo "$CLAUDE_CONFIG_DIR"'], capture_output=True, text=True,
                check=True, env={"BLISS_DIR": tmp, "HOME": tmp, "PATH": os.environ["PATH"]})
            config = root / "agent-config"
            self.assertEqual(result.stdout.strip(), str(config))
            self.assertEqual((config / ".credentials.json").read_text(), "sent")
            self.assertEqual(stat.S_IMODE((config / ".credentials.json").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o700)
            self.assertEqual(home_login.read_text(), "the remote user's own")

    def test_a_refreshed_token_survives_a_restart(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "credential-auth.json").write_text("original")
            (root / "credential-read-only").write_text("x")
            (root / "credential-read-only").chmod(0o400)  # as a Secret mounts it
            (root / "agent-config").mkdir()
            (root / "agent-config/auth.json").write_text("refreshed")
            subprocess.run(["bash", "-c", _credentials_script(BUILTIN["pi"], "$BLISS_DIR/agent-config")],
                           check=True, env={"BLISS_DIR": tmp, "PATH": os.environ["PATH"]})
            self.assertEqual((root / "agent-config/auth.json").read_text(), "refreshed")
            self.assertEqual(stat.S_IMODE((root / "agent-config/read-only").stat().st_mode), 0o600)

    def test_kubernetes_keeps_it_in_the_secret_under_a_valid_key(self):
        scripts = self.workload({".credentials.json": "hunter2"}).scripts(
            GUIX, cache_prefix="$HOME/c", sandboxed=True)
        secret, pod = manifests("n", "i", scripts, {})
        self.assertNotIn("hunter2", json.dumps(pod))
        for key in secret["data"]:
            self.assertRegex(key, r"^[-._a-zA-Z0-9]+$")
            self.assertNotIn("..", key)  # Kubernetes rejects keys containing it


class SystemImageTest(unittest.TestCase):
    def load(self, loaded_name):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return mock.Mock(returncode=1)  # `image inspect`: not loaded yet

        with mock.patch("bliss.targets.image.run", fake_run), \
                mock.patch("bliss.targets.image.output",
                           return_value=f"Loaded image: {loaded_name}"):
            ensure_system_image(["podman"], Pack(format="docker-archive", key="k", path="p"))
        return [cmd for cmd in calls if cmd[1] == "rmi"]

    def test_nix_image_is_not_removed_as_its_own_alias(self):
        self.assertEqual(self.load("localhost/bliss-sys:k"), [])
        self.assertEqual(self.load("bliss-sys:k"), [])

    def test_guix_chosen_name_is_dropped(self):
        self.assertEqual(self.load("localhost/bliss-sys.abc:latest"),
                         [["podman", "rmi", "localhost/bliss-sys.abc:latest"]])


class GcTest(unittest.TestCase):
    def gc(self, dry_run):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / ".bliss"
            for item in ("packs/used", "packs/old/ro", "cache/agents/claude-1", "cache/agents/claude-2",
                         "workloads/w"):
                (root / item).mkdir(parents=True)
            (root / "packs/old/ro").chmod(0o500)
            (root / "packs/old").chmod(0o500)
            (root / "workloads/w/keep").write_text("packs/used\ncache/agents/claude-1\n")
            prefix = "BLISS_GC_DRY_RUN=1\n" if dry_run else "BLISS_GC_DRY_RUN=\n"
            subprocess.run(["bash", "-c", prefix + GC_SCRIPT], check=True, capture_output=True,
                           env={"HOME": tmp, "PATH": "/usr/bin:/bin:" + os.environ["PATH"]})
            found = sorted(str(p.relative_to(root)) for parent in ("packs", "cache/agents")
                           for p in (root / parent).iterdir())
            for path in root.rglob("*"):
                if path.is_dir():
                    path.chmod(0o700)
            return found

    def test_removes_only_what_no_workload_keeps(self):
        self.assertEqual(self.gc(False), ["cache/agents/claude-1", "packs/used"])

    def test_dry_run_removes_nothing(self):
        self.assertEqual(len(self.gc(True)), 4)

    def test_image_in_use_is_kept(self):
        system = {"used": ["a", "b"], "old": ["c"]}
        self.assertEqual(unused_system_images(system, [["a", "b", "x"]]), ["old"])


class TmuxLogsTest(unittest.TestCase):
    def test_trailing_blank_lines_are_dropped(self):
        command = tmux_logs_command("n").split(" | ", 1)[1]
        result = subprocess.run(["bash", "-c", command], input="one\n\ntwo\n\n\n\n",
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout, "one\n\ntwo\n")


class NixTest(unittest.TestCase):
    def test_roots_from_env(self):
        a = "/nix/store/" + "a" * 32 + "-hello-2.12"
        b = "/nix/store/" + "b" * 32 + "-gcc-13"
        missing = "/nix/store/" + "d" * 32 + "-shell"
        environ = {"PATH": f"{a}/bin:/usr/bin", "CC": f"{b}/bin/gcc", "out": missing,
                   "NIX_PROFILES": "/nix/store/" + "c" * 32 + "-p"}
        with mock.patch("os.path.exists", lambda path: path != missing):
            roots = nix_shell_roots(environ)
        self.assertEqual(roots, sorted([a, b]))

    def test_docker_archive_shape(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "pkg"
            fake.mkdir()
            (fake / "hello").write_text("hi")
            dest = root / "a.tar"
            write_docker_archive(dest, [str(fake)], [str(fake)], "/nix/store/l/ld.so", "bliss-sys:k")
            with tarfile.open(dest) as tar:
                manifest = json.load(tar.extractfile("manifest.json"))[0]
                self.assertEqual(manifest["RepoTags"], ["bliss-sys:k"])
                layer = tarfile.open(fileobj=tar.extractfile(manifest["Layers"][0]))
                names = layer.getnames()
                self.assertEqual(layer.getmember("lib64/ld-linux-x86-64.so.2").linkname,
                                 "/nix/store/l/ld.so")
            self.assertIn("usr/bin/env", names)
            self.assertIn("bin/hello", names)


if __name__ == "__main__":
    unittest.main()
