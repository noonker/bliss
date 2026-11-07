"""What every target needs: the workload description and streaming helpers."""

from __future__ import annotations

import io
import os
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

from ..capture import env_script, tar_directory
from ..runtime import CREDENTIAL_PREFIX, run_script
from ..system import AgentSpec, Pack
from ..util import BlissError, tool, tool_path


@dataclass
class Workload:
    id: str
    root: Path
    command: list[str]
    env: dict[str, str]
    spec: AgentSpec | None = None
    unattended: bool = False
    credentials: dict[str, str] = field(default_factory=dict)

    def scripts(self, pack: Pack, *, cache_prefix: str, keep_session: bool = False,
                sandboxed: bool = False, credentials_dir: str = "$HOME/.bliss-agent-config",
                ) -> dict[str, str]:
        """The small files every target delivers beside the system.

        `sandboxed` says the workload is confined to a container or pod.
        `credentials_dir` is where the agent's copy of its login goes; it must
        be writable and go away with the workload.
        """
        extra = dict(self.spec.agent.sandbox_env) if self.spec and self.unattended and sandboxed else {}
        return {
            "env.sh": env_script(self.env),
            "run.sh": run_script(pack, self.command, spec=self.spec,
                                 cache_prefix=cache_prefix, keep_session=keep_session,
                                 extra_env=extra,
                                 credentials_dir=credentials_dir if self.credentials else None),
            **{CREDENTIAL_PREFIX + name: text for name, text in self.credentials.items()},
        }


def bytes_tar(files: dict[str, str], prefix: str = "", mode: int = 0o600) -> bytes:
    """A tar of in-memory text files, private by default since env.sh is secret."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        if prefix:
            info = tarfile.TarInfo(prefix)
            info.type = tarfile.DIRTYPE
            info.mode = 0o700
            tar.addfile(info)
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"{prefix}/{name}" if prefix else name)
            info.size = len(data)
            info.mode = mode
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def stream_files_to(root: Path, cmd: list[str]) -> tuple[int, int]:
    """Pipe the working directory's tar into a command's stdin."""
    proc = subprocess.Popen([tool(cmd[0]), *cmd[1:]], stdin=subprocess.PIPE,
                            env={**os.environ, "PATH": tool_path()})
    assert proc.stdin
    try:
        result = tar_directory(root, proc.stdin)
    except BrokenPipeError:
        result = (0, 0)
    finally:
        try:
            proc.stdin.close()
        except BrokenPipeError:
            pass
    if proc.wait() != 0:
        raise BlissError(f"sending files failed: {' '.join(cmd[:3])}")
    return result


def pull_tar(cmd: list[str], dest: Path, strip: int = 0) -> None:
    """Unpack a tar produced by a command, refusing unsafe member paths."""
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([tool(cmd[0]), *cmd[1:]], stdout=subprocess.PIPE,
                            env={**os.environ, "PATH": tool_path()})
    assert proc.stdout
    with tarfile.open(fileobj=proc.stdout, mode="r|*") as tar:
        for member in tar:
            if strip:
                parts = member.name.split("/")[strip:]
                if not parts:
                    continue
                member.name = "/".join(parts)
            tar.extract(member, dest, filter="data")
    if proc.wait() != 0:
        raise BlissError(f"pull failed: {' '.join(cmd[:3])}")
