"""This machine, in place: same sessions and commands, nothing packed or copied."""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from ..capture import env_script
from ..runtime import in_place_script
from ..util import BlissError, has_tool, log, run, state_dir, tool
from .base import Workload
from .ssh import SshTarget


class HereTarget(SshTarget):
    """An ssh target whose shell is local and whose work directory is the real one."""

    in_place = True

    def __init__(self, name: str, spec: dict):
        super().__init__(name, {**spec, "host": "localhost"})
        # A server of its own, so that nothing here touches the user's sessions,
        # and so that it is the same server from inside or outside another tmux.
        self.tmux = "tmux -L bliss"

    def _remote(self, script: str, *, tty: bool = False, connect_timeout: int | None = None) -> list[str]:
        return [tool("bash"), "-c", f"unset TMUX TMUX_PANE STY WINDOW; {script}"]

    def launch(self, system, workload: Workload) -> dict:
        if not has_tool(self.session):
            raise BlissError(f"{self.session} is not installed on this machine")
        directory = state_dir() / "workloads" / workload.id
        directory.mkdir(parents=True, mode=0o700)
        directory.chmod(0o700)
        bash = tool("bash")
        _write_private(directory / "env.sh", env_script(workload.env))
        _write_private(directory / "run.sh", in_place_script(workload.command, bash))
        inner = (f"BLISS_DIR={shlex.quote(str(directory))} "
                 f"BLISS_WORK={shlex.quote(str(workload.root))} "
                 f"{shlex.quote(bash)} {shlex.quote(str(directory / 'run.sh'))}")
        name = self._session_name(workload.id)
        if self.session == "tmux":
            start = f"{self.tmux} new-session -d -s {name} {shlex.quote(inner)}"
        else:
            start = f"screen -dmS {name} sh -c {shlex.quote(inner)}"
        run(self._remote(start))
        return {"session": name, "dir": str(directory)}

    def pull(self, record: dict, dest: Path) -> None:
        raise BlissError(f"{record['id']} works in {record['cwd']}; there is nothing to copy")

    def gc(self, dry_run: bool) -> None:
        log("nothing is cached for this target")


def _write_private(path: Path, text: str) -> None:
    """env.sh holds secrets, so it is private from the moment it exists."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(text)
