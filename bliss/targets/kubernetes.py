"""A pod or simple Deployment. The image must be pushed to a registry."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from ..system import System
from ..util import BlissError, exec_tool, has_tool, log, run
from .base import Workload, pull_tar
from .image import IMAGE_CACHE, WORKDIR, build_workload_image, gc_system_images


def k8s_name(wid: str) -> str:
    name = re.sub(r"[^a-z0-9-]", "-", f"bliss-{wid}".lower())
    return name[:63].rstrip("-")


def pull_image(image: str, registry: str, spec: dict) -> str:
    """The image as the cluster names it, which may differ from the pushed name."""
    pull_registry = spec.get("pull_registry")
    if not pull_registry:
        return image
    return pull_registry.rstrip("/") + image[len(registry.rstrip("/")):]


def manifests(name: str, image: str, scripts: dict[str, str], spec: dict) -> list[dict]:
    """env.sh is a Secret and files are in the image, so a restart keeps both."""
    labels = {"app.kubernetes.io/managed-by": "bliss", "bliss/workload": name}
    namespace = spec.get("namespace")
    meta = {"name": name, "labels": labels, **({"namespace": namespace} if namespace else {})}
    secret = {
        "apiVersion": "v1", "kind": "Secret", "metadata": meta, "type": "Opaque",
        "data": {key: base64.b64encode(text.encode()).decode()
                 for key, text in scripts.items()},
    }
    container = {
        "name": "workload", "image": image, "imagePullPolicy": "IfNotPresent",
        "command": ["/bin/bash", "/bliss/run.sh"], "workingDir": WORKDIR,
        "tty": True, "stdin": True,
        "env": [{"name": "BLISS_DIR", "value": "/bliss"}, {"name": "BLISS_ROOT", "value": "/"},
                {"name": "BLISS_WORK", "value": WORKDIR}],
        "volumeMounts": [{"name": "bliss", "mountPath": "/bliss", "readOnly": True}],
    }
    if "resources" in spec:
        container["resources"] = spec["resources"]
    pod_spec: dict = {
        "containers": [container],
        "volumes": [{"name": "bliss", "secret": {"secretName": name, "defaultMode": 0o400}}],
    }
    if "image_pull_secret" in spec:
        pod_spec["imagePullSecrets"] = [{"name": spec["image_pull_secret"]}]
    if spec.get("kind", "pod") == "deployment":
        workload = {
            "apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta,
            "spec": {"replicas": 1, "selector": {"matchLabels": {"bliss/workload": name}},
                     "template": {"metadata": {"labels": labels}, "spec": pod_spec}},
        }
    else:
        pod_spec["restartPolicy"] = "Never"
        workload = {"apiVersion": "v1", "kind": "Pod", "metadata": meta, "spec": pod_spec}
    return [secret, workload]


class KubernetesTarget:
    def __init__(self, name: str, spec: dict):
        self.name = name
        self.spec = spec
        self.engine = list(spec.get("builder", ["podman" if has_tool("podman") else "docker"]))

    def kubectl(self) -> list[str]:
        cmd = ["kubectl"]
        if "context" in self.spec:
            cmd += ["--context", self.spec["context"]]
        if "namespace" in self.spec:
            cmd += ["--namespace", self.spec["namespace"]]
        return cmd

    def _ref(self, record: dict) -> str:
        if self.spec.get("kind", "pod") == "deployment":
            return f"deployment/{record['name']}"
        return f"pod/{record['name']}"

    def launch(self, system: System, workload: Workload) -> dict:
        registry = self.spec.get("registry")
        if not registry:
            raise BlissError(f"target '{self.name}' needs 'registry' in its settings: the "
                             "cluster pulls the workload image from there")
        name = k8s_name(workload.id)
        image = f"{registry.rstrip('/')}/bliss-work:{workload.id}"
        pack = system.build("docker-archive", workload.spec)
        build_workload_image(self.engine, pack, workload, image)
        log(f"pushing {image}; it contains this directory's files, so the "
            "registry can read anything not in .blissignore")
        run([*self.engine, "push", *self.spec.get("push_args", []), image], quiet=True)
        scripts = workload.scripts(pack, cache_prefix=IMAGE_CACHE, sandboxed=True)
        objects = manifests(name, pull_image(image, registry, self.spec), scripts, self.spec)
        document = json.dumps({"apiVersion": "v1", "kind": "List", "items": objects})
        run([*self.kubectl(), "apply", "-f", "-"], input=document.encode(), quiet=True)
        return {"name": name, "image": image}

    def status(self, record: dict) -> str:
        proc = run([*self.kubectl(), "get", "pods", "-l", f"bliss/workload={record['name']}",
                    "-o", "jsonpath={.items[*].status.phase}"], capture=True, check=False)
        return (proc.stdout.decode().strip() or "gone") if proc.returncode == 0 else "unknown"

    def attach(self, record: dict) -> int:
        return exec_tool([*self.kubectl(), "attach", "-it", self._ref(record)])

    def logs(self, record: dict, follow: bool) -> int:
        return exec_tool([*self.kubectl(), "logs", *(["-f"] if follow else []), self._ref(record)])

    def pull(self, record: dict, dest: Path) -> None:
        pull_tar([*self.kubectl(), "exec", self._ref(record), "--",
                  "tar", "cf", "-", "-C", WORKDIR, "."], dest)

    def stop(self, record: dict) -> None:
        kind = "deployment" if self.spec.get("kind", "pod") == "deployment" else "pod"
        run([*self.kubectl(), "delete", kind, record["name"], "--ignore-not-found", "--wait=false"],
            check=False, quiet=True)
        run([*self.kubectl(), "delete", "secret", record["name"], "--ignore-not-found"],
            check=False, quiet=True)
        run([*self.engine, "rmi", record["image"]], check=False, quiet=True)

    def gc(self, dry_run: bool) -> None:
        gc_system_images(self.engine, dry_run)
        log(f"images already pushed to {self.spec.get('registry')} are not touched; "
            "remove those with the registry's own tools")
