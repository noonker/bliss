"""Build a workload image: system pack, then agent, then files."""

from __future__ import annotations

import json
import subprocess
import tarfile
import tempfile
from pathlib import Path

from ..capture import tar_directory
from ..runtime import image_install_script
from ..system import Pack
from ..util import BlissError, log, output, run
from .base import Workload

# Installed agents live here in images, so an image layer is their cache.
IMAGE_CACHE = "/opt/bliss/agents"
WORKDIR = "/work"

_PASSWD = "root:x:0:0:root:/root:/bin/bash\nnobody:x:65534:65534:nobody:/:/bin/false\n"
_GROUP = "root:x:0:\nnogroup:x:65534:\n"


def ensure_system_image(engine: list[str], pack: Pack) -> str:
    """Load the pack once; its content hash names the image."""
    tag = f"bliss-sys:{pack.key}"
    if run([*engine, "image", "inspect", tag], check=False, quiet=True).returncode == 0:
        return tag
    log(f"loading system image {tag}")
    loaded = output([*engine, "load", "-i", str(pack.path)])
    names = [line.split(":", 1)[1].strip() for line in loaded.splitlines()
             if line.startswith("Loaded image")]
    if not names:
        raise BlissError(f"could not find the image name in load output: {loaded}")
    run([*engine, "tag", names[-1], tag], quiet=True)
    if _short_name(names[-1]) != tag:
        # Guix names the image itself; only the content-addressed tag is ours to find.
        run([*engine, "rmi", names[-1]], check=False, quiet=True)
    return tag


def _short_name(image: str) -> str:
    """An engine may report `bliss-sys:x` as `localhost/bliss-sys:x`: the same image."""
    return image.removeprefix("localhost/").removeprefix("docker.io/library/")


def _layers(engine: list[str], image: str) -> list[str]:
    text = output([*engine, "image", "inspect", "-f", "{{json .RootFS.Layers}}", image])
    return json.loads(text) or []


def unused_system_images(system: dict[str, list[str]], work: list[list[str]]) -> list[str]:
    """System images (name -> layers) that no workload image is built on."""
    return sorted(name for name, layers in system.items()
                  if not any(built[:len(layers)] == layers for built in work))


def gc_system_images(engine: list[str], dry_run: bool) -> None:
    """Remove system images that no workload image is built on."""
    listing = output([*engine, "images", "--format", "{{.Repository}}:{{.Tag}}"]).splitlines()
    named = {"bliss-sys": [], "bliss-work": []}
    for image in listing:
        base = image.rsplit("/", 1)[-1]
        for prefix, found in named.items():
            if base.startswith(prefix):
                found.append(image)
    system = {image: _layers(engine, image) for image in named["bliss-sys"]}
    work = [_layers(engine, image) for image in named["bliss-work"]]
    unused = unused_system_images(system, work)
    for image in unused:
        log(f"{'would remove' if dry_run else 'removing'} {image}")
        if not dry_run:
            run([*engine, "rmi", image], check=False, quiet=True)
    log(f"kept {len(system) - len(unused)} system image(s) in use")


def containerfile(base: str, workload: Workload, pack: Pack) -> str:
    lines = [f"FROM {base}", "COPY etc/ /etc/"]
    if workload.spec and workload.spec.agent.kind == "installer":
        # Before the files, so changing them keeps the installed agent's layer.
        lines += ["COPY bliss-build/ /bliss-build/",
                  f'RUN ["{pack.bash}", "/bliss-build/install-agent.sh"]']
    lines += [f"COPY work/ {WORKDIR}/", f"WORKDIR {WORKDIR}", "ENV HOME=/root"]
    return "\n".join(lines) + "\n"


def build_workload_image(engine: list[str], pack: Pack, workload: Workload, tag: str,
                         extra_args: list[str] | None = None) -> None:
    base = ensure_system_image(engine, pack)
    with tempfile.TemporaryDirectory(prefix="bliss-") as tmp:
        context = Path(tmp)
        (context / "etc").mkdir()
        (context / "etc/passwd").write_text(_PASSWD)
        (context / "etc/group").write_text(_GROUP)
        if workload.spec and workload.spec.agent.kind == "installer":
            (context / "bliss-build").mkdir()
            (context / "bliss-build/install-agent.sh").write_text(
                image_install_script(workload.spec, pack, IMAGE_CACHE))
        work = context / "work"
        work.mkdir()
        tar_path = context / "work.tar"
        with open(tar_path, "wb") as handle:
            count, size = tar_directory(workload.root, handle)
        with tarfile.open(tar_path) as tar:
            tar.extractall(work, filter="fully_trusted")
        tar_path.unlink()
        (context / "Containerfile").write_text(containerfile(base, workload, pack))
        log(f"building image with {count} files ({size / 1e6:.1f} MB)")
        run([*engine, "build", "-q", "-t", tag, "-f", str(context / "Containerfile"),
             *(extra_args or []), str(context)], stdout=subprocess.DEVNULL)
