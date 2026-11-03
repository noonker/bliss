"""Settings file, remembered default target, and the workload ledger."""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .util import BlissError, state_dir

TARGET_TYPES = {"podman", "docker", "ssh", "kubernetes"}


def config_path() -> Path:
    override = os.environ.get("BLISS_CONFIG")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "bliss" / "config.toml"


@dataclass
class Config:
    targets: dict[str, dict]
    env: dict
    ai: dict
    agents: dict[str, dict]
    path: Path

    def target(self, name: str) -> dict:
        if name not in self.targets:
            known = ", ".join(sorted(self.targets)) or "none"
            raise BlissError(f"unknown target '{name}' (configured: {known}); "
                             f"targets are defined in {self.path}")
        return self.targets[name]


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    data: dict = {}
    if path.exists():
        try:
            data = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as err:
            raise BlissError(f"{path}: {err}") from err
    targets = data.get("targets", {})
    for name, spec in targets.items():
        kind = spec.get("type")
        if kind not in TARGET_TYPES:
            raise BlissError(f"target '{name}': type must be one of "
                             f"{sorted(TARGET_TYPES)}, got {kind!r}")
        if kind == "ssh" and "host" not in spec:
            raise BlissError(f"target '{name}': ssh targets need 'host'")
        if kind == "ssh" and spec.get("session", "tmux") not in ("tmux", "screen"):
            raise BlissError(f"target '{name}': session must be tmux or screen")
        if kind == "kubernetes" and spec.get("kind", "pod") not in ("pod", "deployment"):
            raise BlissError(f"target '{name}': kind must be pod or deployment")
    return Config(targets=targets, env=data.get("env", {}), ai=data.get("ai", {}),
                  agents=data.get("agents", {}), path=path)


def _state_file() -> Path:
    return state_dir() / "state.json"


def _read_state() -> dict:
    try:
        return json.loads(_state_file().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_state(state: dict) -> None:
    path = _state_file()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


def get_default_target() -> str | None:
    return _read_state().get("default_target")


def set_default_target(name: str) -> None:
    state = _read_state()
    state["default_target"] = name
    _write_state(state)


def record_workload(workload: dict) -> None:
    state = _read_state()
    state.setdefault("workloads", []).append(workload)
    _write_state(state)


def forget_workload(wid: str) -> None:
    state = _read_state()
    state["workloads"] = [w for w in state.get("workloads", []) if w["id"] != wid]
    _write_state(state)


def list_workloads() -> list[dict]:
    return _read_state().get("workloads", [])


def newest_here(workloads: list[dict], cwd: str) -> dict | None:
    """What a bare `bliss attach` means in this directory."""
    return next((w for w in reversed(workloads) if w["cwd"] == cwd), None)


def _matches(workload: dict, query: str) -> bool:
    """`sunny-otter` answers to `sunny-otter`, `sun` and `ott`."""
    return any(part.startswith(query) for part in (workload["id"], *workload["id"].split("-")))


def find_workload(target: str | None, wid: str | None, cwd: str) -> dict:
    """By name or the start of either word; else the newest from this directory.

    With nothing sent from this directory, the only workload there is also counts.
    """
    workloads = [w for w in list_workloads() if target is None or w["target"] == target]
    if wid and wid != ".":
        for w in workloads:
            if w["id"] == wid:
                return w
        found = [w for w in workloads if _matches(w, wid)]
        if len(found) == 1:
            return found[0]
        if found:
            names = ", ".join(w["id"] for w in found)
            raise BlissError(f"'{wid}' could be any of: {names}")
        raise BlissError(f"no workload matches '{wid}'; see 'bliss list'")
    here = newest_here(workloads, cwd)
    if here:
        return here
    if wid is None and len(workloads) == 1:
        return workloads[0]
    raise BlissError("no workload from this directory; pass a name (see 'bliss list')")
