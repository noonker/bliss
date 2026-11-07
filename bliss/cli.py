"""bliss [--target T] <action>; `bliss --target T` alone sets the default."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import __version__, config as cfg
from .agents import Release, agent_command, get_agent, read_credentials, resolve
from .capture import capture_env, here_env
from .names import new_name
from .system import AgentSpec, detect_system
from .targets import get_target
from .targets.base import Workload
from .util import BlissError, has_tool, log


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bliss", description="Send this directory, its env and its Guix/Nix shell "
                                  "to podman, a remote tmux/screen session, or Kubernetes.",
        epilog="With --target and no action, sets the default target.")
    parser.add_argument("--target", "-t", help="target name from the settings file")
    parser.add_argument("--version", action="version", version=f"bliss {__version__}")
    actions = parser.add_subparsers(dest="action", metavar="<action>")

    run = actions.add_parser("run", help="run a command on the target (default: a shell)")
    run.add_argument("--attach", "-a", action="store_true", help="attach after starting")
    run.add_argument("--dry-run", "-n", action="store_true", help="show the plan; send nothing")
    run.add_argument("--json", action="store_true",
                     help="print the started workload as JSON, for other tools")
    run.add_argument("--allow-untracked", action="store_true",
                     help="send even if the directory is not inside a git repository")
    run.add_argument("command", nargs=argparse.REMAINDER, help="command (after --)")

    ai = actions.add_parser("ai", help="run an AI agent on the target")
    ai.add_argument("--agent", help="claude, autolith, hermes or pi (default: [ai] agent)")
    ai.add_argument("--attach", "-a", action="store_true", help="attach after starting")
    ai.add_argument("--dry-run", "-n", action="store_true", help="show the plan; send nothing")
    ai.add_argument("--refresh", action="store_true", help="check for a new agent release now")
    ai.add_argument("--json", action="store_true",
                    help="print the started workload as JSON, for other tools")
    ai.add_argument("--unattended", action="store_true", default=None,
                    help="let the agent act without approval prompts")
    ai.add_argument("--allow-untracked", action="store_true",
                    help="send even if the directory is not inside a git repository")
    ai.add_argument("prompt", nargs="*", help="task; omit for an interactive session")

    which = ("workload name, or the start of either word: 'ott' for sunny-otter "
             "(default, or '.': newest from this directory)")
    for name, text, aliases in (("attach", "attach to a workload", ["a"]),
                                ("status", "show a workload's state", []),
                                ("stop", "stop a workload and remove it from the target", [])):
        sub = actions.add_parser(name, help=text, aliases=aliases)
        sub.add_argument("id", nargs="?", metavar="name", help=which)
        if name == "status":
            sub.add_argument("--json", action="store_true", help="print JSON, for other tools")
    logs = actions.add_parser("logs", help="show a workload's output")
    logs.add_argument("id", nargs="?", metavar="name", help=which)
    logs.add_argument("--follow", "-f", action="store_true")
    pull = actions.add_parser("pull", help="copy a workload's files back")
    pull.add_argument("id", nargs="?", metavar="name", help=which)
    pull.add_argument("--dest", help="directory to write (default: .bliss/pull/<id>)")
    gc = actions.add_parser("gc", help="remove cached systems and agents no workload uses")
    gc.add_argument("--dry-run", "-n", action="store_true", help="show what would go; remove nothing")
    listing = actions.add_parser("list", aliases=["ls"], help="list workloads and their state")
    listing.add_argument("--json", action="store_true", help="print JSON, for other tools")
    targets = actions.add_parser("targets", help="list configured targets")
    targets.add_argument("--json", action="store_true", help="print JSON, for other tools")
    return parser


def _target_name(args, config: cfg.Config) -> str:
    name = args.target or cfg.get_default_target()
    if not name:
        raise BlissError("no target given and no default set; "
                         "use --target <name> (see 'bliss targets')")
    config.target(name)
    return name


def _is_in_place(target_spec: dict) -> bool:
    return target_spec["type"] == "here"


def _assert_git_tracked(root: Path) -> None:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise BlissError(
            f"{root} is not inside a git repository; bliss refuses to send untracked "
            "directories by default (use --allow-untracked or set "
            "allow_untracked_dir = true in your config to override)"
        )


def _print_targets(config: cfg.Config) -> None:
    default = cfg.get_default_target()
    if not config.targets:
        print(f"no targets configured; add them to {config.path}")
    for name, spec in sorted(config.targets.items()):
        mark = "*" if name == default else " "
        print(f"{mark} {name:<16} {spec['type']}")


def _print_plan(target: str, spec: dict, system, workload: Workload) -> None:
    print(f"target:  {target} ({spec['type']})")
    print(f"system:  {system.describe() if system else 'this machine, as it is'}")
    print(f"files:   {workload.root} " + ("(see .blissignore)" if system else "(in place; nothing is copied)"))
    print(f"command: {workload.command}")
    if workload.spec:
        release = workload.spec.release
        print(f"agent:   {workload.spec.agent.name} {release.version} ({workload.spec.agent.kind})")
        for path in workload.spec.agent.credentials if system else ():
            print(f"login:   {path} (copied to the target; see [agents] credentials)")
    print(f"env ({len(workload.env)} vars, values not shown):")
    for name in sorted(workload.env):
        print(f"  {name}")


def _launch(args, config: cfg.Config, command: list[str], spec: AgentSpec | None,
            attach: bool, unattended: bool = False) -> int:
    name = _target_name(args, config)
    target_spec = config.target(name)
    target = get_target(name, target_spec)
    in_place = _is_in_place(target_spec)
    if spec and not in_place and platform.machine() != "x86_64":
        raise BlissError(f"AI agents are only packed for x86_64, not {platform.machine()}")
    root = Path.cwd()
    if not in_place:
        allow = getattr(args, "allow_untracked", False) or config.allow_untracked_dir
        if not allow:
            _assert_git_tracked(root)
    if in_place:
        system = None
        env = here_env(dict(os.environ))
    else:
        system = detect_system(dict(os.environ))
        env = capture_env(dict(os.environ), profile=system.profile(), closure=system.closure(),
                          cwd=str(root), settings=config.env)
    workload = Workload(id=new_name({w["id"] for w in cfg.list_workloads()}), root=root, command=command, env=env, spec=spec,
                        unattended=unattended,
                        # Here, the agent already has the login that is on this machine.
                        credentials=read_credentials(spec.agent) if spec and not in_place else {})
    if args.dry_run:
        _print_plan(name, target_spec, system, workload)
        return 0
    if workload.credentials:
        log(f"sending your {spec.agent.name} login to {name}: "
            + ", ".join(sorted(workload.credentials)))
    details = target.launch(system, workload)
    record = {"id": workload.id, "target": name, "cwd": str(root), "time": time.time(),
              "command": command, **details}
    cfg.record_workload(record)
    log(f"started {workload.id} on {name}")
    if getattr(args, "json", False):
        print(json.dumps({"name": workload.id, "target": name}), flush=True)
    if attach:
        return target.attach(record)
    log(f"attach: bliss attach {workload.id}   logs: bliss logs {workload.id}   "
        f"files back: bliss pull {workload.id}   stop: bliss stop {workload.id}")
    return 0


def _cmd_run(args, config: cfg.Config) -> int:
    command = [c for c in args.command]
    if command[:1] == ["--"]:
        command = command[1:]
    interactive = not command
    return _launch(args, config, command or ["bash", "-i"], None, args.attach or interactive)


def _cmd_ai(args, config: cfg.Config) -> int:
    agent = get_agent(config, args.agent)
    prompt = " ".join(args.prompt) or None
    unattended = args.unattended if args.unattended is not None else config.ai.get("unattended", False)
    command = agent_command(agent, prompt, unattended)
    if unattended and command[0] != agent.name:
        log(f"warning: this {agent.name} command is a wrapper, so its unattended flags were not added")
    if _is_in_place(config.target(_target_name(args, config))):
        # Whatever is on PATH is what runs, so there is nothing to fetch or pin.
        if not has_tool(agent.name):
            raise BlissError(f"{agent.name} is not installed on this machine; the 'here' target "
                             "runs what is on your PATH and does not download agents")
        if unattended:
            log(f"warning: {agent.name} will act without approval prompts directly on this "
                "machine, with nothing sandboxing it")
        spec = AgentSpec(agent, Release("installed", "", ""))
    else:
        spec = AgentSpec(agent, resolve(agent, refresh=args.refresh))
    return _launch(args, config, command, spec, args.attach or not prompt, unattended)


# States with no process left. An ssh workload that "exited(N)" keeps its shell.
_FINISHED = {"gone", "exited", "stopped", "Succeeded", "Failed"}


def _status_json(record: dict, state: str) -> dict:
    """`finished` is whether the command has ended, even if a shell is kept open after it."""
    match = re.fullmatch(r"exited\((-?\d+)\)", state)
    exit_code = int(match.group(1)) if match else {"Succeeded": 0}.get(state)
    return {"name": record["id"], "target": record["target"], "state": state,
            "finished": state in _FINISHED or match is not None, "exit": exit_code}


def _manage(args, config: cfg.Config) -> int:
    record = cfg.find_workload(args.target, args.id, str(Path.cwd()))
    target = get_target(record["target"], config.target(record["target"]))
    if args.action == "attach":
        state = target.status(record)
        if state in _FINISHED:
            raise BlissError(f"{record['id']} is {state}, so there is nothing to attach to; "
                             f"see 'bliss logs {record['id']}', or 'bliss stop {record['id']}' "
                             "to clear it")
        return target.attach(record)
    if args.action == "logs":
        return target.logs(record, args.follow)
    if args.action == "status":
        state = target.status(record)
        if args.json:
            print(json.dumps(_status_json(record, state)))
        else:
            print(f"{record['id']}: {state}")
        return 0
    if args.action == "pull" and _is_in_place(config.target(record["target"])):
        log(f"{record['id']} works in {record['cwd']}; there is nothing to copy")
        return 0
    if args.action == "pull":
        dest = Path(args.dest) if args.dest else Path.cwd() / ".bliss" / "pull" / record["id"]
        target.pull(record, dest)
        log(f"files written to {dest}")
        return 0
    target.stop(record)
    cfg.forget_workload(record["id"])
    log(f"stopped {record['id']}")
    return 0


def _age(seconds: float) -> str:
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{int(seconds // size)}{unit}"
    return f"{int(seconds)}s"


def _state(record: dict, config: cfg.Config) -> str:
    """Never fails: one unreachable target must not hide the rest of the list."""
    try:
        return get_target(record["target"], config.target(record["target"])).status(record)
    except BlissError:
        return "unknown"


def _record_in_place(record: dict, config: cfg.Config) -> bool:
    spec = config.targets.get(record["target"])
    return bool(spec) and _is_in_place(spec)


def _cmd_list(args, config: cfg.Config) -> int:
    records = cfg.list_workloads()
    if not records and not args.json:
        print("no workloads; start one with 'bliss run' or 'bliss ai'")
        return 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        states = list(pool.map(lambda r: _state(r, config), records))
    here = cfg.newest_here(records, str(Path.cwd()))
    if args.json:
        print(json.dumps([{"name": record["id"], "target": record["target"], "state": state,
                           "started": record["time"], "directory": record["cwd"],
                           "command": record.get("command", []), "here": record is here,
                           "in_place": _record_in_place(record, config)}
                          for record, state in zip(records, states)]))
        return 0
    rows = [("", "NAME", "TARGET", "STATE", "AGE", "DIRECTORY")]
    for record, state in zip(records, states):
        directory = record["cwd"].replace(os.path.expanduser("~"), "~", 1)
        rows.append(("*" if record is here else "", record["id"], record["target"], state,
                     _age(time.time() - record["time"]), directory))
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())
    if here:
        print(f"\n* bare 'bliss attach' here means {here['id']}")
    return 0


def _cmd_gc(args, config: cfg.Config) -> int:
    name = _target_name(args, config)
    get_target(name, config.target(name)).gc(args.dry_run)
    return 0


def dispatch(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.action = {"ls": "list", "a": "attach"}.get(args.action, args.action)
    config = cfg.load_config()
    if args.action is None:
        if args.target:
            config.target(args.target)
            cfg.set_default_target(args.target)
            log(f"default target is now {args.target}")
            return 0
        _print_targets(config)
        return 0
    if args.action == "targets":
        if args.json:
            default = cfg.get_default_target()
            print(json.dumps([{"name": name, "type": spec["type"], "default": name == default}
                              for name, spec in sorted(config.targets.items())]))
            return 0
        _print_targets(config)
        return 0
    if args.action == "list":
        return _cmd_list(args, config)
    if args.action == "gc":
        return _cmd_gc(args, config)
    if args.action == "run":
        return _cmd_run(args, config)
    if args.action == "ai":
        return _cmd_ai(args, config)
    return _manage(args, config)


def main(argv: list[str] | None = None) -> int:
    try:
        return dispatch(sys.argv[1:] if argv is None else argv)
    except BlissError as err:
        print(f"bliss: error: {err}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
