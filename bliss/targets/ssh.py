"""A remote Linux server, in a tmux or screen session the user can attach to."""

from __future__ import annotations

import platform
import shlex
from pathlib import Path

from ..runtime import agent_cache
from ..system import Pack, System
from ..util import BlissError, exec_tool, log, output, run
from .base import Workload, bytes_tar, pull_tar, stream_files_to

REMOTE_ROOT = ".bliss"

# Prints each decision; a pack or agent is kept if a workload's `keep` file names it.
GC_SCRIPT = f"""
cd "$HOME/{REMOTE_ROOT}" 2>/dev/null || exit 0
keep=$(cat workloads/*/keep 2>/dev/null)
for item in packs/* cache/agents/*; do
  [ -e "$item" ] || continue
  if printf '%s\\n' "$keep" | grep -qxF -- "$item"; then
    echo "keeping $item"
    continue
  fi
  if [ -n "$BLISS_GC_DRY_RUN" ]; then
    echo "would remove $item ($(du -sh "$item" | cut -f1))"
  else
    echo "removing $item ($(du -sh "$item" | cut -f1))"
    chmod -R u+w "$item"
    rm -rf "$item"
  fi
done
"""


def tmux_logs_command(name: str) -> str:
    """The whole pane; tmux pads the bottom with blank lines, so drop those."""
    return (f"tmux capture-pane -p -J -S - -t ={name}: | "
            "sed -e :a -e '/^\\n*$/{$d;N;ba' -e '}'")


class SshTarget:
    def __init__(self, name: str, spec: dict):
        self.name = name
        self.spec = spec
        self.host = spec["host"]
        self.session = spec.get("session", "tmux")
        self.ssh = [*spec.get("ssh_command", ["ssh"]), *spec.get("ssh_args", [])]

    def _remote(self, script: str, *, tty: bool = False) -> list[str]:
        # ssh joins its arguments with spaces and hands them to the remote shell.
        return [*self.ssh, *(["-t"] if tty else []), self.host, script]

    def _dir(self, wid: str) -> str:
        return f"$HOME/{REMOTE_ROOT}/workloads/{wid}"

    def _send_pack(self, pack: Pack) -> str:
        """Unpack the system once per pack hash, so later runs only send files."""
        pack_dir = f"$HOME/{REMOTE_ROOT}/packs/{pack.key}"
        have = run(self._remote(f'test -e "{pack_dir}/.ok"'), check=False, quiet=True)
        if have.returncode == 0:
            return pack_dir
        log(f"sending system pack to {self.host}")
        with open(str(pack.path), "rb") as handle:
            run(self._remote(f'mkdir -p "{pack_dir}.tmp" && tar xzf - -C "{pack_dir}.tmp" '
                             f'&& touch "{pack_dir}.tmp/.ok" && rm -rf "{pack_dir}" '
                             f'&& mv "{pack_dir}.tmp" "{pack_dir}"'), stdin=handle)
        return pack_dir

    def _copy_closure(self, pack: Pack) -> None:
        check = run(self._remote("command -v nix-store"), check=False, quiet=True)
        if check.returncode != 0:
            raise BlissError(f"{self.host} has no Nix. From a Nix shell, ssh targets need "
                             "Nix on the remote; podman and kubernetes targets do not.")
        log(f"copying nix closure to {self.host}")
        run(["nix", "--extra-experimental-features", "nix-command", "copy",
             "--to", f"ssh://{self.host}", *pack.store_paths])

    def _session_name(self, wid: str) -> str:
        return f"bliss-{wid}"

    def launch(self, system: System, workload: Workload) -> dict:
        session = self.session
        probe = run(self._remote(f"command -v {session}"), check=False, quiet=True)
        if probe.returncode != 0:
            raise BlissError(f"{session} is not installed on {self.host}")
        remote_arch = output(self._remote("uname -m"))
        if remote_arch != platform.machine():
            raise BlissError(f"{self.host} is {remote_arch} but this machine is "
                             f"{platform.machine()}; the system pack would not run there")
        pack = system.build("relocatable", workload.spec)
        directory = self._dir(workload.id)
        work = f"{directory}/work"
        # Written first, so a concurrent `bliss gc` cannot take what this is about to use.
        keep = [f"packs/{pack.key}"]
        if workload.spec:
            keep.append(agent_cache(workload.spec.agent, workload.spec.release, "cache/agents"))
        run(self._remote(f'mkdir -p "{work}" && chmod 700 "{directory}" && '
                         f'cat > "{directory}/keep"'), input="\n".join(keep).encode() + b"\n")
        if pack.format == "nix-closure":
            self._copy_closure(pack)
            bash = pack.bash
            root_env = ""
        else:
            pack_dir = self._send_pack(pack)
            bash = f"{pack_dir}/bin/bash"
            root_env = f'BLISS_ROOT="{pack_dir}" '
        scripts = workload.scripts(pack, cache_prefix=f"$HOME/{REMOTE_ROOT}/cache/agents",
                                   keep_session=True)
        run(self._remote(f'tar xf - -C "{directory}"'), input=bytes_tar(scripts))
        count, size = stream_files_to(workload.root, self._remote(f'tar xf - -C "{work}"'))
        log(f"sent {count} files ({size / 1e6:.1f} MB)")
        name = self._session_name(workload.id)
        inner = (f'{root_env}BLISS_DIR="{directory}" BLISS_WORK="{work}" '
                 f'"{bash}" "{directory}/run.sh"')
        if session == "tmux":
            start = f"tmux new-session -d -s {name} {shlex.quote(inner)}"
        else:
            start = f"screen -dmS {name} sh -c {shlex.quote(inner)}"
        run(self._remote(start))
        return {"session": name, "dir": directory}

    def _alive_check(self, name: str) -> str:
        if self.session == "tmux":
            return f"tmux has-session -t ={name} 2>/dev/null"
        return f"screen -ls {name} 2>/dev/null | grep -q {name}"

    def status(self, record: dict) -> str:
        script = (f'if {self._alive_check(record["session"])}; then '
                  f'if [ -e "{record["dir"]}/exit" ]; then '
                  f'echo "exited($(cat "{record["dir"]}/exit"))"; else echo running; fi; '
                  f"else echo gone; fi")
        # Quick to give up, so a dead host does not stall 'bliss list'.
        proc = run([*self.ssh, "-o", "ConnectTimeout=5", self.host, script],
                   capture=True, check=False)
        return proc.stdout.decode().strip() if proc.returncode == 0 else "unknown"

    def attach(self, record: dict) -> int:
        name = record["session"]
        cmd = f"tmux attach -t ={name}" if self.session == "tmux" else f"screen -r {name}"
        return exec_tool(self._remote(cmd, tty=True))

    def logs(self, record: dict, follow: bool) -> int:
        name = record["session"]
        if follow:
            log("--follow is not supported on ssh targets; showing the output so far "
                "(use 'bliss attach' to watch it)")
        if self.session == "tmux":
            return exec_tool(self._remote(tmux_logs_command(name)))
        out = f'{record["dir"]}/screen.log'
        return exec_tool(self._remote(f'screen -S {name} -X hardcopy -h "{out}" && sleep 1 && cat "{out}"'))

    def pull(self, record: dict, dest: Path) -> None:
        pull_tar(self._remote(f'tar cf - -C "{record["dir"]}/work" .'), dest)

    def stop(self, record: dict) -> None:
        name = record["session"]
        kill = f"tmux kill-session -t ={name}" if self.session == "tmux" else f"screen -S {name} -X quit"
        run(self._remote(f'{kill} 2>/dev/null; rm -rf "{record["dir"]}"'), check=False, quiet=True)

    def gc(self, dry_run: bool) -> None:
        prefix = "BLISS_GC_DRY_RUN=1; " if dry_run else "BLISS_GC_DRY_RUN=; "
        run(self._remote(prefix + GC_SCRIPT))
