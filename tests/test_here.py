"""Runs real sessions on a private tmux server, so needs tmux, and skips without it."""

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from bliss.capture import here_env
from bliss.targets.base import Workload
from bliss.targets.here import HereTarget


class HereEnvTest(unittest.TestCase):
    def test_keeps_what_a_workload_could_need_and_drops_the_terminal(self):
        env = here_env({"PATH": "/a", "HOME": "/h", "SSH_AUTH_SOCK": "/s", "TOKEN": "t",
                        "TMUX": "x", "TERM": "dumb", "PWD": "/p"})
        self.assertEqual(env, {"PATH": "/a", "HOME": "/h", "SSH_AUTH_SOCK": "/s", "TOKEN": "t"})


@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
class HereSessionTest(unittest.TestCase):
    def setUp(self):
        # A short path: tmux sockets cannot live under a long one.
        tmp = tempfile.TemporaryDirectory(prefix="blsh", dir="/tmp")
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.work = self.dir / "my project"
        self.work.mkdir()
        (self.work / "keep.txt").write_text("mine")
        patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.dir / "state"),
                                               "TMUX_TMPDIR": str(self.dir)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TMUX", None)
        self.target = HereTarget("here", {"type": "here"})

    def launch(self, command, env=None):
        record = self.target.launch(None, Workload(id="sunny-otter", root=self.work, command=command,
                                                   env={"PATH": os.environ["PATH"], **(env or {})}))
        record["id"] = "sunny-otter"
        self.addCleanup(self.target.stop, record)
        return record

    def wait_for(self, record, wanted):
        for _ in range(100):
            state = self.target.status(record)
            if state == wanted:
                return
            time.sleep(0.05)
        self.fail(f"state stayed {state}")

    def test_runs_in_the_real_directory_with_the_environment(self):
        record = self.launch(["bash", "-c", 'echo "$MARK:$PWD" > seen; exit 4'], {"MARK": "it's here"})
        self.wait_for(record, "exited(4)")
        self.assertEqual((self.work / "seen").read_text().strip(), f"it's here:{self.work}")

    def test_env_file_is_private(self):
        record = self.launch(["sleep", "30"])
        self.assertEqual((Path(record["dir"]) / "env.sh").stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(record["dir"]).stat().st_mode & 0o777, 0o700)

    def test_stop_removes_the_session_and_state_but_not_the_directory(self):
        record = self.launch(["sleep", "30"])
        self.wait_for(record, "running")
        self.target.stop(record)
        self.assertEqual(self.target.status(record), "gone")
        self.assertFalse(Path(record["dir"]).exists())
        self.assertEqual((self.work / "keep.txt").read_text(), "mine")

    def test_session_is_not_on_the_users_tmux_server(self):
        self.assertIn("-L bliss", self.target.tmux)
        self.assertIn("-L bliss", self.target._alive_check("x"))


if __name__ == "__main__":
    unittest.main()
