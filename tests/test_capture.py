import io
import subprocess
import tarfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from bliss.capture import Ignore, capture_env, env_script, tar_directory

PROFILE = "/gnu/store/aaaa-profile"


def capture(environ, closure=(), cwd="/home/me/proj", settings=None):
    env = {"HOME": "/home/me", **environ}
    return capture_env(env, profile=PROFILE, closure=set(closure), cwd=cwd, settings=settings)


def evaluate(script, expr, profile="/P", work="/W"):
    """Run a generated env script in a real shell."""
    return subprocess.run(
        ["bash", "-c", f'BLISS_PROFILE={profile}; BLISS_WORK={work}; {script}\nprintf %s "{expr}"'],
        capture_output=True, text=True, check=True).stdout


class EnvTest(unittest.TestCase):
    def test_session_vars_are_dropped(self):
        env = capture({"SSH_AUTH_SOCK": "/run/x", "DISPLAY": ":0", "FOO": "bar"})
        self.assertEqual(env, {"FOO": "bar"})

    def test_secrets_travel_unless_excluded(self):
        self.assertIn("API_KEY", capture({"API_KEY": "s"}))
        self.assertNotIn("API_KEY", capture({"API_KEY": "s"}, settings={"exclude": ["*_KEY"]}))

    def test_include_overrides_builtin_drop(self):
        self.assertIn("DISPLAY", capture({"DISPLAY": ":0"}, settings={"include": ["DISPLAY"]}))

    def test_host_paths_removed_from_lists(self):
        env = capture({"X_PATH": f"/home/me/x:{PROFILE}/lib:/gnu/store/gone-pkg/lib:/opt/keep"})
        script = env_script(env)
        self.assertEqual(evaluate(script, "$X_PATH"), "/P/lib:/opt/keep")

    def test_store_path_in_closure_is_kept(self):
        env = capture({"X": "/gnu/store/bbb-pkg/share"}, closure={"/gnu/store/bbb-pkg"})
        self.assertEqual(env["X"], "/gnu/store/bbb-pkg/share")

    def test_var_of_only_host_paths_is_dropped(self):
        self.assertNotIn("X", capture({"X": "/home/me/only"}))

    def test_cwd_rewritten_only_at_path_boundary(self):
        env = capture({"A": "/home/me/proj/sub", "B": "/home/me/project"})
        script = env_script(env)
        self.assertEqual(evaluate(script, "$A"), "/W/sub")
        # /home/me/project is under $HOME and is not the working directory.
        self.assertNotIn("B", env)

    def test_hostile_values_survive_quoting(self):
        value = "it's $(touch /nonexistent) `x` \"q\" \n newline"
        script = env_script(capture({"V": value}))
        self.assertEqual(evaluate(script, "$V"), value)

    def test_invalid_names_are_skipped(self):
        self.assertNotIn("BAD-NAME", env_script({"BAD-NAME": "x", "OK": "y"}))


class IgnoreTest(unittest.TestCase):
    def test_patterns(self):
        ignore = Ignore(["*.log", "build/", "/docs/*.tmp", "# comment", ""])
        self.assertTrue(ignore.matches("a/b.log", False))
        self.assertTrue(ignore.matches("x/build", True))
        self.assertFalse(ignore.matches("x/build", False))
        self.assertTrue(ignore.matches("docs/a.tmp", False))
        self.assertFalse(ignore.matches("other/a.tmp", False))


class TarTest(unittest.TestCase):
    def test_contents(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "keep").write_text("k")
            (root / "drop.log").write_text("d")
            (root / ".git").mkdir()
            (root / ".git/HEAD").write_text("h")
            (root / "skipdir").mkdir()
            (root / "skipdir/x").write_text("x")
            (root / "link").symlink_to("keep")
            (root / ".bliss").mkdir()
            (root / ".bliss/old").write_text("o")
            (root / ".blissignore").write_text("*.log\nskipdir/\n")
            subprocess.run(["mkfifo", str(root / "fifo")], check=True)
            buffer = io.BytesIO()
            tar_directory(root, buffer)
            buffer.seek(0)
            with tarfile.open(fileobj=buffer) as tar:
                names = set(tar.getnames())
                self.assertTrue(tar.getmember("link").issym())
        self.assertEqual(names, {"keep", "link", ".git", ".git/HEAD", ".blissignore"})


if __name__ == "__main__":
    unittest.main()
