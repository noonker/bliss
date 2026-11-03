"""bliss [--target T] <action>; `bliss --target T` alone sets the default."""

from __future__ import annotations

import argparse
import os
import platform
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import __version__, config as cfg
from .agents import agent_command, get_agent, resolve
from .capture import capture_env
from .names import new_name
from .system import AgentSpec, detect_system
from .targets import get_target
from .targets.base import Workload
from .util import BlissError, log


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
    run.add_argument("command", nargs=argparse.REMAINDER, help="command (after --)")

    ai = actions.add_parser("ai", help="run an AI agent on the target")
    ai.add_argument("--agent", help="claude, autolith, hermes or pi (default: [ai] agent)")
    ai.add_argument("--attach", "-a", action="store_true", help="attach after starting")
    ai.add_argument("--dry-run", "-n", action="store_true", help="show the plan; send nothing")
    ai.add_argument("--refresh", action="store_true", help="check for a new agent release now")
    ai.add_argument("--unattended", action="store_true", default=None,
                    help="let the agent act without approval prompts")
    ai.add_argument("prompt", nargs="*", help="task; omit for an interactive session")

    which = ("workload name, or the start of either word: 'ott' for sunny-otter "
             "(default, or '.': newest from this directory)")
    for name, text, aliases in (("attach", "attach to a workload", ["a"]),
                                ("status", "show a workload's state", []),
                                ("stop", "stop a workload and remove it from the target", [])):
        sub = actions.add_parser(name, help=text, aliases=aliases)
        sub.add_argument("id", nargs="?", metavar="name", help=which)
    logs = actions.add_parser("logs", help="show a workload's output")
    logs.add_argument("id", nargs="?", metavar="name", help=which)
    logs.add_argument("--follow", "-f", action="store_true")
    pull = actions.add_parser("pull", help="copy a workload's files back")
    pull.add_argument("id", nargs="?", metavar="name", help=which)
    pull.add_argument("--dest", help="directory to write (default: .bliss/pull/<id>)")
    gc = actions.add_parser("gc", help="remove cached systems and agents no workload uses")
    gc.add_argument("--dry-run", "-n", action="store_true", help="show what would go; remove nothing")
    actions.add_parser("list", aliases=["ls"], help="list workloads and their state")
    actions.add_parser("targets", help="list configured targets")
    return parser


def _target_name(args, config: cfg.Config) -> str:
    name = args.target or cfg.get_default_target()
    if not name:
        raise BlissError("no target given and no default set; "
                         "use --target <name> (see 'bliss targets')")
    config.target(name)
    return name


def _print_targets(config: cfg.Config) -> None:
    default = cfg.get_default_target()
    if not config.targets:
        print(f"no targets configured; add them to {config.path}")
    for name, spec in sorted(config.targets.items()):
        mark = "*" if name == default else " "
        print(f"{mark} {name:<16} {spec['type']}")


def _print_plan(target: str, spec: dict, system, workload: Workload) -> None:
    print(f"target:  {target} ({spec['type']})")
    print(f"system:  {system.describe()}")
    print(f"files:   {workload.root} (see .blissignore)")
    print(f"command: {workload.command}")
    if workload.spec:
        release = workload.spec.release
        print(f"agent:   {workload.spec.agent.name} {release.version} ({workload.spec.agent.kind})")
    print(f"env ({len(workload.env)} vars, values not shown):")
    for name in sorted(workload.env):
        print(f"  {name}")


def _launch(args, config: cfg.Config, command: list[str], spec: AgentSpec | None,
            attach: bool, unattended: bool = False) -> int:
    name = _target_name(args, config)
    if spec and platform.machine() != "x86_64":
        raise BlissError(f"AI agents are only packed for x86_64, not {platform.machine()}")
    target_spec = config.target(name)
    target = get_target(name, target_spec)
    root = Path.cwd()
    system = detect_system(dict(os.environ))
    env = capture_env(dict(os.environ), profile=system.profile(), closure=system.closure(),
                      cwd=str(root), settings=config.env)
    workload = Workload(id=new_name({w["id"] for w in cfg.list_workloads()}), root=root, command=command, env=env, spec=spec,
                        unattended=unattended)
    if args.dry_run:
        _print_plan(name, target_spec, system, workload)
        return 0
    details = target.launch(system, workload)
    record = {"id": workload.id, "target": name, "cwd": str(root), "time": time.time(),
              "command": command, **details}
    cfg.record_workload(record)
    log(f"started {workload.id} on {name}")
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
    spec = AgentSpec(agent, resolve(agent, refresh=args.refresh))
    return _launch(args, config, command, spec, args.attach or not prompt, unattended)


# States with no process left. An ssh workload that "exited(N)" keeps its shell.
_FINISHED = {"gone", "exited", "stopped", "Succeeded", "Failed"}


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
        print(f"{record['id']}: {target.status(record)}")
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


def _cmd_list(config: cfg.Config) -> int:
    records = cfg.list_workloads()
    if not records:
        print("no workloads; start one with 'bliss run' or 'bliss ai'")
        return 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        states = list(pool.map(lambda r: _state(r, config), records))
    here = cfg.newest_here(records, str(Path.cwd()))
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
        _print_targets(config)
        return 0
    if args.action == "list":
        return _cmd_list(config)
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
