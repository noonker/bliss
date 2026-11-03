import base64
import json
import os
import subprocess
import tarfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from bliss.agents import BUILTIN, Release
from bliss.runtime import _native_libs_script, prepare_agent_script, run_script
from bliss.system import AgentSpec, Pack, guix_manifest, nix_shell_roots, write_docker_archive
from bliss.targets.base import Workload
from bliss.targets.image import unused_system_images
from bliss.targets.kubernetes import k8s_name, manifests
from bliss.targets.ssh import GC_SCRIPT, tmux_logs_command

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
                        ca_file="c", bash="/bin/bash")
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
            (release / "pi").write_text("#!/bin/sh\necho ran\n")
            (release / "pi").chmod(0o755)
            archive = root / "pi.tar.gz"
            subprocess.run(["tar", "czf", archive, "-C", tmp, "pi"], check=True)
            (root / "env.sh").write_text("")
            pack = Pack(format="nix-closure", key="k", agent_root=str(archive),
                        bin_dirs=(), ca_file="c", patchelf="true", loader="l", bash="/bin/bash")
            spec = AgentSpec(BUILTIN["pi"], Release("v1", "u", "s"))
            script = run_script(pack, ["pi"], spec=spec, cache_prefix=f"{tmp}/cache")
            env = {"BLISS_DIR": tmp, "BLISS_WORK": tmp, "HOME": tmp, "PATH": os.environ["PATH"]}
            for _ in range(2):  # the second run reuses the unpacked copy
                result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                        check=True, env=env)
                self.assertEqual(result.stdout, "ran\n")

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
