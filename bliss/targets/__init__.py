"""Delivery backends. Each takes a Workload and manages it through its life."""

from __future__ import annotations

from ..util import BlissError


def get_target(name: str, spec: dict):
    kind = spec["type"]
    if kind in ("podman", "docker"):
        from .container import ContainerTarget
        return ContainerTarget(name, spec)
    if kind == "ssh":
        from .ssh import SshTarget
        return SshTarget(name, spec)
    if kind == "here":
        from .here import HereTarget
        return HereTarget(name, spec)
    if kind == "kubernetes":
        from .kubernetes import KubernetesTarget
        return KubernetesTarget(name, spec)
    raise BlissError(f"unknown target type {kind}")
