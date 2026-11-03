"""Small helpers shared across bliss."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


class BlissError(Exception):
    """An expected failure; shown to the user without a traceback."""


# `guix shell --pure` drops most of PATH, but bliss still needs guix, ssh and
# friends, so fall back to the places Guix and Nix keep user tools.
_FALLBACK_DIRS = [
    "~/.config/guix/current/bin",
    "~/.guix-home/profile/bin",
    "~/.guix-profile/bin",
    "/run/current-system/profile/bin",
    "~/.nix-profile/bin",
    "/etc/profiles/per-user/{user}/bin",
    "/run/current-system/sw/bin",
    "/nix/var/nix/profiles/default/bin",
    "~/bin",
    "~/.local/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
]


def tool_path() -> str:
    user = os.environ.get("USER", "")
    dirs = [os.path.expanduser(d.format(user=user)) for d in _FALLBACK_DIRS]
    return os.pathsep.join([os.environ.get("PATH", ""), *dirs])


def tool(name: str) -> str:
    """Absolute path of an external tool, or a BlissError saying it is missing."""
    found = shutil.which(name, path=tool_path())
    if not found:
        raise BlissError(f"required tool not found: {name}")
    return found


def has_tool(name: str) -> bool:
    return shutil.which(name, path=tool_path()) is not None


def log(message: str) -> None:
    print(f"bliss: {message}", file=sys.stderr, flush=True)


def run(cmd: list[str], *, capture: bool = False, input: bytes | None = None,
        check: bool = True, env: dict[str, str] | None = None,
        stdin=None, stdout=None, quiet: bool = False,
        ) -> subprocess.CompletedProcess:
    """Run a tool. On failure, raise a BlissError carrying its stderr."""
    argv = [tool(cmd[0]), *cmd[1:]]
    full_env = {**os.environ, **(env or {}), "PATH": tool_path()}
    if os.environ.get("BLISS_DEBUG"):
        log("+ " + shlex.join(cmd))
    kwargs = {}
    if capture:
        kwargs["stdout"] = subprocess.PIPE
    elif stdout is not None:
        kwargs["stdout"] = stdout
    elif quiet:
        kwargs["stdout"] = subprocess.DEVNULL
    if capture or quiet:
        kwargs["stderr"] = subprocess.PIPE
    if input is not None:
        kwargs["input"] = input
    elif stdin is not None:
        kwargs["stdin"] = stdin
    try:
        proc = subprocess.run(argv, env=full_env, **kwargs)
    except OSError as err:
        raise BlissError(f"cannot run {cmd[0]}: {err}") from err
    if check and proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip() if proc.stderr else ""
        message = f"command failed ({proc.returncode}): {shlex.join(cmd[:4])}"
        raise BlissError(f"{message}\n{detail}" if detail else message)
    return proc


def output(cmd: list[str], **kwargs) -> str:
    return run(cmd, capture=True, **kwargs).stdout.decode().strip()


def exec_tool(cmd: list[str]) -> int:
    """Hand the terminal to a tool (attach, logs) and return its exit code."""
    try:
        return subprocess.run([tool(cmd[0]), *cmd[1:]],
                              env={**os.environ, "PATH": tool_path()}).returncode
    except OSError as err:
        raise BlissError(f"cannot run {cmd[0]}: {err}") from err


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    path = Path(base) / "bliss"
    path.mkdir(parents=True, exist_ok=True)
    return path


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    path = Path(base) / "bliss"
    path.mkdir(parents=True, exist_ok=True)
    return path
