"""The scripts that run on the target. They need only bash and the pack."""

from __future__ import annotations

import shlex
import textwrap

from .agents import Agent, Release
from .system import LOADER, AgentSpec, Pack


_EXIT_ON_FAILURE = 'bliss_fail() { exit "$1"; }\n'
# In a session, keep the pane: otherwise the error vanishes along with it.
_KEEP_FAILURE = textwrap.dedent("""\
    bliss_fail() {
      echo "$1" > "$BLISS_DIR/exit"
      echo "[bliss] setup failed with status $1; this is a shell for looking into it"
      exec bash -i
    }
    """)


CREDENTIAL_PREFIX = "credential-"


def _dq(expr: str) -> str:
    """Double-quote a shell expression so its $VARS still expand."""
    return '"' + expr.replace("\\", "\\\\").replace('"', '\\"') + '"'


def prelude(pack: Pack) -> str:
    """Locate the system: sets BLISS_PROFILE, PATH, HOME and the CA bundle."""
    lines = []
    if pack.uses_profile:
        # BLISS_ROOT/bin links to <profile>/bin. Under a relocated root the
        # physical path has a prefix, but /gnu/store is what the namespace shows.
        lines.append(textwrap.dedent("""\
            p=$(cd -P "${BLISS_ROOT:-/}/bin" && pwd -P)
            p=${p%/bin}
            export BLISS_PROFILE="/gnu/store/${p##*/gnu/store/}"
            export GUIX_ENVIRONMENT="$BLISS_PROFILE"
            export GUIX_LOCPATH="$BLISS_PROFILE/lib/locale"
            unset p"""))
    bins = ":".join(pack.bin_dirs)
    lines.append(f'export PATH={_dq(bins)}"${{PATH:+:$PATH}}"')
    lines.append(textwrap.dedent("""\
        : "${HOME:=/root}"
        export HOME
        mkdir -p "$HOME" 2>/dev/null
        mkdir -p /tmp 2>/dev/null
        export TERM="${TERM:-xterm-256color}\""""))
    ca = _dq(pack.ca_file)
    lines.append(f"export SSL_CERT_FILE={ca} CURL_CA_BUNDLE={ca} GIT_SSL_CAINFO={ca} "
                 f"NIX_SSL_CERT_FILE={ca}")
    return "\n".join(lines) + "\n"


def agent_cache(agent: Agent, release: Release, prefix: str) -> str:
    return f"{prefix}/{agent.name}-{release.version}"


def install_agent_script(spec: AgentSpec, cache: str) -> str:
    """Run an in-place installer once, publishing its result atomically."""
    agent = spec.agent
    args = list(agent.installer_args)
    if agent.version:
        args += ["--commit", agent.version]
    joined = " ".join(shlex.quote(a) for a in args)
    return textwrap.dedent(f"""\
        cache={_dq(cache)}
        if [ ! -e "$cache/.ok" ]; then
          echo "bliss: installing {agent.name} into $cache" >&2
          mkdir -p "$cache"
          # Its own HOME: the installer otherwise edits ~/.bashrc and ~/.profile.
          mkdir -p "$cache/.home"
          # Saved first: in `curl | bash` a failed download looks like success.
          curl -fsSL {shlex.quote(agent.installer_url or "")} -o "$cache/install.sh" || bliss_fail $?
          # A relocatable pack runs in a user namespace, where tar cannot chown.
          HOME="$cache/.home" SHELL=/bin/bash HERMES_HOME="$cache" TAR_OPTIONS=--no-same-owner \\
            bash "$cache/install.sh" {joined} \\
              --dir "$cache/hermes-agent" --hermes-home "$cache" || bliss_fail $?
          touch "$cache/.ok"
        fi
        """)


def _native_libs_script(agent: Agent) -> str:
    """A directory with only the agent's libraries: the profile's lib also has a
    glibc, which would break any host program the agent runs."""
    if not agent.native_libs:
        return ""
    patterns = " ".join(f'"$BLISS_PROFILE"/lib/{pattern}' for pattern in agent.native_libs)
    return textwrap.dedent(f"""\
        libs="$HOME/.bliss/lib/{agent.name}"
        mkdir -p "$libs"
        for f in {patterns}; do
          if [ -e "$f" ]; then ln -sf "$f" "$libs/"; fi
        done
        export LD_LIBRARY_PATH="$libs"
        """)


def _unpack_agent_script(agent: Agent, pack: Pack, cache: str) -> str:
    """A Nix store holds a release tarball as fetched, so unpack it once here."""
    patch = textwrap.indent(_patch_script(pack), "            ")
    return textwrap.dedent(f"""\
        unset LD_LIBRARY_PATH
        cache={_dq(cache)}
        if [ ! -e "$cache/.ok" ]; then
          tmp="$cache.tmp.$$"
          mkdir -p "$tmp"
          tar xzf {_dq(pack.agent_root)} -C "$tmp" --strip-components=1 || bliss_fail $?
          if [ ! -e {LOADER} ]; then
{patch}
          fi
          touch "$tmp/.ok"
          mv "$tmp" "$cache"
        fi
        export PATH="$cache/{agent.bin_rel}:$PATH"
        """)


def _patch_script(pack: Pack) -> str:
    """Point every executable in $tmp at the pack's loader."""
    return textwrap.dedent(f"""\
        find "$tmp" -type f | while read -r f; do
          if {_dq(pack.patchelf)} --print-interpreter "$f" >/dev/null 2>&1; then
            {_dq(pack.patchelf)} --set-interpreter {_dq(pack.loader)} "$f"
          fi
        done""")


def prepare_agent_script(spec: AgentSpec, pack: Pack, cache_prefix: str) -> str:
    """Make the agent runnable and put it first on PATH.

    Release binaries run unmodified wherever there is an FHS loader. Only a host
    without one (Guix System, NixOS) gets a copy with the interpreter rewritten,
    which is cached there. RUNPATH stays untouched: rewriting it breaks Bun
    executables such as Claude Code, and glibc's loader finds its own libraries.
    """
    agent, release = spec.agent, spec.release
    cache = agent_cache(agent, release, cache_prefix)
    if agent.kind == "installer":
        return ("unset LD_LIBRARY_PATH\n" + _native_libs_script(agent)
                + install_agent_script(spec, cache)
                + 'export HERMES_HOME="$cache"\n'
                + f'export PATH="$cache/{agent.venv_bin}:$PATH"\n')
    if not pack.uses_profile and agent.flake:
        return "unset LD_LIBRARY_PATH\n"  # built by Nix, nothing foreign to run
    if agent.kind == "tarball" and not pack.uses_profile:
        return _unpack_agent_script(agent, pack, cache)
    # In a relocatable pack the profile holds a wrapper that re-enters the
    # namespace, and patching that breaks it. The release itself is the store
    # item with the exact name; the wrapper's name has an extra "R". Grafting can
    # leave a second copy of the same release, and either one will do.
    source = "$release" if pack.uses_profile else pack.agent_root
    find_release = (f'for release in /gnu/store/*-bliss-agent-{agent.name}-'
                    f'{release.version.lstrip("v")}; do break; done'
                    if pack.uses_profile else ":")
    # A profile holds the package, whereas Nix fetches the bare executable.
    is_flat = not pack.uses_profile
    source_dir = f"{source}/libexec/{agent.name}" if agent.kind == "tarball" else ""
    if agent.kind == "binary" and is_flat:
        link = (f'mkdir -p "$HOME/.bliss/bin"\n'
                f'ln -sf {_dq(source)} "$HOME/.bliss/bin/{agent.name}"\n'
                f'export PATH="$HOME/.bliss/bin:$PATH"\n')
    else:
        link = ""
    if agent.kind == "binary":
        copy = f'cp -L {_dq(source + ("" if is_flat else f"/bin/{agent.name}"))} "$tmp/{agent.name}"'
        bin_rel = ""
    else:
        copy = f'cp -rL {_dq(source_dir)}/. "$tmp/"'
        bin_rel = agent.bin_rel
    patch = textwrap.indent(_patch_script(pack), "            ")
    return link + textwrap.dedent(f"""\
        unset LD_LIBRARY_PATH
        if [ ! -e {LOADER} ]; then
          cache={_dq(cache)}
          if [ ! -e "$cache/.ok" ]; then
            echo "bliss: no {LOADER} here; patching a cached copy of {agent.name}" >&2
            tmp="$cache.tmp.$$"
            mkdir -p "$tmp"
            {find_release}
            {copy} || bliss_fail $?
            chmod -R u+w "$tmp"
{patch}
            touch "$tmp/.ok"
            mv "$tmp" "$cache"
          fi
          export PATH="$cache/{bin_rel}:$PATH"
        fi
        """) + _native_libs_script(agent)


def _append_to_path(bin_dirs: tuple[str, ...]) -> str:
    """env.sh may set PATH to the shell's; keep the pack's tools reachable, once."""
    return "".join(f'case ":$PATH:" in *:{_dq(d)}:*) ;; *) PATH="$PATH":{_dq(d)} ;; esac\n'
                   for d in bin_dirs)


def _credentials_script(agent: Agent, config_dir: str) -> str:
    """Point the agent at a private, writable config directory holding the files.

    A copy, because a Kubernetes Secret is read-only and agents refresh their
    tokens. An existing file is kept, so a restarted pod keeps a refreshed token.
    """
    return textwrap.dedent(f"""\
        config={_dq(config_dir)}
        (umask 077; mkdir -p "$config")
        for f in "$BLISS_DIR"/{CREDENTIAL_PREFIX}*; do
          name=${{f##*/{CREDENTIAL_PREFIX}}}
          if [ ! -e "$config/$name" ]; then
            (umask 077; cp -L "$f" "$config/$name"; chmod 600 "$config/$name")
          fi
        done
        export {agent.config_env}="$config"
        """)


def _keep_session_tail(run_line: str, bash: str = "bash") -> str:
    """Keep the pane alive so output can be read and `bliss status` knows why."""
    return textwrap.dedent(f"""\
        {run_line}
        rc=$?
        echo "$rc" > "$BLISS_DIR/exit"
        echo "[bliss] command exited with status $rc; this is a shell in the same environment"
        exec {shlex.quote(bash)} -i
        """)


def in_place_script(command: list[str], bash: str) -> str:
    """run.sh for this machine: no system to find, nothing to install.

    `bash` is a full path, since the environment may be a pure shell without it.
    """
    return ('. "$BLISS_DIR/env.sh"\ncd "$BLISS_WORK" || exit 1\n'
            + _keep_session_tail(shlex.join(command), bash))


def run_script(pack: Pack, command: list[str], *, spec: AgentSpec | None,
               cache_prefix: str, keep_session: bool = False,
               extra_env: dict[str, str] | None = None,
               credentials_dir: str | None = None) -> str:
    """run.sh: expects BLISS_DIR, BLISS_WORK and (for a profile) BLISS_ROOT."""
    parts = ["#!/bin/bash\n", _KEEP_FAILURE if keep_session else _EXIT_ON_FAILURE,
             prelude(pack), '. "$BLISS_DIR/env.sh"\n',
             _append_to_path(pack.bin_dirs), "export PATH\n"]
    parts += [f"export {key}={shlex.quote(value)}\n" for key, value in (extra_env or {}).items()]
    if spec:
        parts.append(prepare_agent_script(spec, pack, cache_prefix))
    if spec and credentials_dir:
        parts.append(_credentials_script(spec.agent, credentials_dir))
    parts.append('cd "$BLISS_WORK" || exit 1\n')
    run_line = shlex.join(command)
    if keep_session:
        parts.append(_keep_session_tail(run_line))
    else:
        parts.append(f"exec {run_line}\n")
    return "".join(parts)


def image_install_script(spec: AgentSpec, pack: Pack, cache_prefix: str) -> str:
    """Image-build step for agents that install in place, cached as a layer."""
    cache = agent_cache(spec.agent, spec.release, cache_prefix)
    return ("#!/bin/bash\n" + _EXIT_ON_FAILURE + prelude(pack) + _native_libs_script(spec.agent)
            + install_agent_script(spec, cache))
