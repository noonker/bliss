"""The AI entrypoints: what each agent is, and where to fetch it.

`resolve()` turns "latest" into a pinned version and checksum, and remembers the
answer. The bytes themselves are cached by the store, as fixed-output items,
or on the target for agents that can only be installed in place.
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace

from .config import Config
from .util import BlissError, cache_dir, log

_CACHE_TTL = 24 * 3600


@dataclass(frozen=True)
class Agent:
    name: str
    # binary:    one executable, pinned by checksum
    # tarball:   a release archive, pinned by checksum
    # installer: only works in place, so it is installed on the target
    kind: str
    interactive: tuple[str, ...]
    prompt: tuple[str, ...]
    unattended: tuple[str, ...] = ()
    # Set only for unattended runs inside a container or pod.
    sandbox_env: tuple[tuple[str, str], ...] = ()
    guix_deps: tuple[str, ...] = ()
    nix_deps: tuple[str, ...] = ()
    flake: str | None = None
    installer_url: str | None = None
    installer_args: tuple[str, ...] = ()
    venv_bin: str | None = None
    version: str | None = None
    # Where a tarball keeps its executable, relative to the unpacked release.
    bin_rel: str = "bin"
    # Libraries it loads at run time. Guix's loader only searches glibc's own
    # directory, so these are made visible, and nothing else from the profile.
    native_libs: tuple[str, ...] = ()
    gcc_runtime: bool = False  # needs libgcc_s and libstdc++ in the pack
    # An in-place install below this much RAM can trigger the host's OOM killer,
    # which then takes down whatever else lives there.
    min_install_memory_mb: int = 0
    # The variable that moves its config directory, which is how credentials
    # reach it without touching a login that already exists on the target.
    config_env: str | None = None
    credentials: tuple[str, ...] = ()


_CLAUDE_BASE = "https://downloads.claude.ai/claude-code-releases"
_AUTOLITH_BASE = "https://sh.lambda-symbolics.com/releases"
_PI_BASE = "https://github.com/earendil-works/pi/releases"

BUILTIN: dict[str, Agent] = {
    "claude": Agent(
        name="claude", kind="binary",
        interactive=("claude",),
        prompt=("claude", "-p", "{prompt}"),
        unattended=("--dangerously-skip-permissions",),
        # Containers run as root, where Claude refuses that flag unless told it
        # is sandboxed. An ssh target is a real machine, so it never gets this.
        sandbox_env=(("IS_SANDBOX", "1"),),
        config_env="CLAUDE_CONFIG_DIR",
        guix_deps=("git", "ripgrep"),
        nix_deps=("git", "ripgrep"),
    ),
    "autolith": Agent(
        name="autolith", kind="tarball",
        interactive=("autolith",),
        # Autolith has no one-shot mode: start a session, then message it.
        prompt=("bash", "-c",
                'autolith --detached && exec autolith lg tell "$1"',
                "autolith", "{prompt}"),
        unattended=("--permissions", "full"),
        guix_deps=("git", "curl", "openssl", "bubblewrap", "gawk", "sed", "grep",
                   "findutils", "ripgrep", "which"),
        nix_deps=("git", "curl", "openssl", "bubblewrap", "which"),
        flake="github:lambda-symbolics/autolith",
        native_libs=("libcrypto.so*", "libssl.so*"),
    ),
    "hermes": Agent(
        name="hermes", kind="installer",
        interactive=("hermes",),
        prompt=("hermes", "chat", "--oneshot", "-q", "{prompt}"),
        unattended=("--yolo",),
        guix_deps=("git", "curl", "gawk", "sed", "grep", "findutils", "xz", "which"),
        nix_deps=("git", "curl", "gawk", "gnused", "gnugrep", "findutils", "xz", "which"),
        installer_url="https://hermes-agent.nousresearch.com/install.sh",
        installer_args=("--non-interactive", "--skip-browser"),
        venv_bin="hermes-agent/.hermes/bin",
        # uv, its Python and native wheels are glibc builds needing these.
        native_libs=("libgcc_s.so*", "libstdc++.so*", "libatomic.so*"),
        gcc_runtime=True,
        min_install_memory_mb=4096,  # it builds a web UI with node
    ),
    "pi": Agent(
        name="pi", kind="tarball",
        interactive=("pi",),
        prompt=("pi", "--print", "{prompt}"),
        # Pi has no approval prompts to turn off, so it is always unattended.
        guix_deps=("git", "ripgrep", "fd"),
        nix_deps=("git", "ripgrep", "fd"),
        bin_rel="",
        config_env="PI_CODING_AGENT_DIR",
    ),
}


def get_agent(config: Config, name: str | None) -> Agent:
    """A built-in agent with any [agents.<name>] overrides applied."""
    name = name or config.ai.get("agent", "claude")
    if name not in BUILTIN:
        raise BlissError(f"unknown agent '{name}' (supported: {', '.join(sorted(BUILTIN))})")
    agent = BUILTIN[name]
    overrides = config.agents.get(name, {})
    changes = {key: tuple(overrides[key])
               for key in ("interactive", "prompt", "unattended") if key in overrides}
    if "min_install_memory_mb" in overrides:
        changes["min_install_memory_mb"] = int(overrides["min_install_memory_mb"])
    if "credentials" in overrides:
        if not agent.config_env:
            raise BlissError(f"[agents.{name}] credentials: {name} has no way to read them "
                             "from a separate directory, so bliss cannot send them safely")
        changes["credentials"] = tuple(overrides["credentials"])
    if "version" in overrides:
        changes["version"] = str(overrides["version"])
    return replace(agent, **changes)


def read_credentials(agent: Agent) -> dict[str, str]:
    """The files the user opted to send, by the name the agent expects."""
    found: dict[str, str] = {}
    for path in agent.credentials:
        full = os.path.expanduser(path)
        name = os.path.basename(full)
        if name in found:
            raise BlissError(f"[agents.{agent.name}] credentials: two files are named {name}")
        try:
            with open(full, encoding="utf-8") as handle:
                found[name] = handle.read()
        except OSError as err:
            raise BlissError(f"[agents.{agent.name}] credentials: cannot read {path}: "
                             f"{err.strerror}") from err
    return found


def agent_command(agent: Agent, prompt: str | None, unattended: bool) -> list[str]:
    """The argv to run, with `{prompt}` substituted as a single argument.

    A template may invoke the agent indirectly (Autolith's goes through bash -c),
    so unattended flags are only inserted when the agent is argv[0].
    """
    template = list(agent.prompt if prompt else agent.interactive)
    command = [prompt if part == "{prompt}" else part for part in template]
    if unattended and command[0] == agent.name:
        command[1:1] = agent.unattended
    return command


@dataclass(frozen=True)
class Release:
    version: str
    url: str
    sha256: str


def _ssl_context() -> ssl.SSLContext:
    """Guix and Nix Pythons often have no default CA store in a pure shell."""
    context = ssl.create_default_context()
    if context.get_ca_certs():
        return context
    for path in (os.environ.get("SSL_CERT_FILE"), os.environ.get("NIX_SSL_CERT_FILE"),
                 "/etc/ssl/certs/ca-certificates.crt", "/etc/ssl/certs/ca-bundle.crt",
                 os.path.expanduser("~/.guix-home/profile/etc/ssl/certs/ca-certificates.crt"),
                 os.path.expanduser("~/.guix-profile/etc/ssl/certs/ca-certificates.crt"),
                 "/run/current-system/profile/etc/ssl/certs/ca-certificates.crt"):
        if path and os.path.exists(path):
            context.load_verify_locations(cafile=path)
            return context
    return context


def _fetch(url: str) -> tuple[str, str]:
    """(body, final URL after redirects)."""
    request = urllib.request.Request(url, headers={"User-Agent": "bliss"})
    with urllib.request.urlopen(request, timeout=30, context=_ssl_context()) as response:
        return response.read().decode().strip(), response.geturl()


def _resolve_claude(version: str | None) -> Release:
    if not version:
        version, _ = _fetch(f"{_CLAUDE_BASE}/latest")
    manifest = json.loads(_fetch(f"{_CLAUDE_BASE}/{version}/manifest.json")[0])
    try:
        checksum = manifest["platforms"]["linux-x64"]["checksum"]
    except KeyError as err:
        raise BlissError(f"claude {version}: manifest has no linux-x64 build") from err
    return Release(version, f"{_CLAUDE_BASE}/{version}/linux-x64/claude", checksum)


def _resolve_autolith(version: str | None) -> Release:
    if not version:
        version = _fetch(f"{_AUTOLITH_BASE}/latest")[1].rsplit("/", 1)[-1]
    archive = f"autolith-{version}-x86_64-linux.tar.gz"
    url = f"{_AUTOLITH_BASE}/{version}/{archive}"
    checksum = _fetch(f"{url}.sha256")[0].split()[0]
    return Release(version, url, checksum)


def _resolve_pi(version: str | None) -> Release:
    if version:
        version = version if version.startswith("v") else f"v{version}"
    else:
        version = _fetch(f"{_PI_BASE}/latest")[1].rsplit("/", 1)[-1]
    archive = "pi-linux-x64.tar.gz"
    for line in _fetch(f"{_PI_BASE}/download/{version}/SHA256SUMS")[0].splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1].lstrip("*") == archive:
            return Release(version, f"{_PI_BASE}/download/{version}/{archive}", fields[0])
    raise BlissError(f"pi {version}: SHA256SUMS has no {archive}")


_RESOLVERS = {"claude": _resolve_claude, "autolith": _resolve_autolith, "pi": _resolve_pi}


def resolve(agent: Agent, *, refresh: bool = False) -> Release:
    """Pin the agent's release, from cache when fresh or when offline."""
    if agent.kind == "installer":
        return Release(agent.version or "stable", agent.installer_url or "", "")
    cache_file = cache_dir() / "agents" / f"{agent.name}-{agent.version or 'latest'}.json"
    cached = None
    if cache_file.exists():
        cached = json.loads(cache_file.read_text())
        fresh = time.time() - cached["time"] < _CACHE_TTL
        if (fresh or agent.version) and not refresh:
            return Release(**cached["release"])
    try:
        release = _RESOLVERS[agent.name](agent.version)
    except (urllib.error.URLError, TimeoutError) as err:
        if cached:
            log(f"cannot check for a new {agent.name} ({err}); using cached version")
            return Release(**cached["release"])
        raise BlissError(f"cannot fetch {agent.name} release info: {err}") from err
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps({"time": time.time(), "release": release.__dict__}))
    return release
