"""podman or docker, on this machine or through a configured connection."""

from __future__ import annotations

from pathlib import Path

from ..system import System
from ..util import exec_tool, run
from .base import Workload, bytes_tar, pull_tar
from .image import IMAGE_CACHE, WORKDIR, build_workload_image, gc_system_images


class ContainerTarget:
    def __init__(self, name: str, spec: dict):
        self.name = name
        self.spec = spec
        # e.g. command = ["podman", "--remote", "--connection", "gpu-box"]
        self.engine = list(spec.get("command", [spec["type"]]))

    def container(self, wid: str) -> str:
        return f"bliss-{wid}"

    def launch(self, system: System, workload: Workload) -> dict:
        pack = system.build("docker-archive", workload.spec)
        name = self.container(workload.id)
        image = f"bliss-work:{workload.id}"
        build_workload_image(self.engine, pack, workload, image)
        run([*self.engine, "create", "-it", "--name", name, "--workdir", WORKDIR,
             "-e", "BLISS_DIR=/bliss", "-e", "BLISS_ROOT=/", "-e", f"BLISS_WORK={WORKDIR}",
             *self.spec.get("run_args", []), image, pack.bash, "/bliss/run.sh"], quiet=True)
        # Delivered after create, so env.sh never becomes part of an image layer.
        scripts = workload.scripts(pack, cache_prefix=IMAGE_CACHE, sandboxed=True)
        run([*self.engine, "cp", "-", f"{name}:/"], input=bytes_tar(scripts, "bliss"))
        run([*self.engine, "start", name], quiet=True)
        return {"container": name, "image": image}

    def status(self, record: dict) -> str:
        proc = run([*self.engine, "inspect", "-f", "{{.State.Status}}", record["container"]],
                   capture=True, check=False)
        return proc.stdout.decode().strip() if proc.returncode == 0 else "gone"

    def attach(self, record: dict) -> int:
        return exec_tool([*self.engine, "attach", record["container"]])

    def logs(self, record: dict, follow: bool) -> int:
        return exec_tool([*self.engine, "logs", *(["-f"] if follow else []), record["container"]])

    def pull(self, record: dict, dest: Path) -> None:
        pull_tar([*self.engine, "cp", f"{record['container']}:{WORKDIR}/.", "-"], dest)

    def stop(self, record: dict) -> None:
        run([*self.engine, "rm", "-f", record["container"]], check=False, quiet=True)
        run([*self.engine, "rmi", record["image"]], check=False, quiet=True)

    def gc(self, dry_run: bool) -> None:
        gc_system_images(self.engine, dry_run)
