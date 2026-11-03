# bliss

Send the current directory, its environment and its Guix or Nix shell to podman
or docker, a tmux or screen session on a remote Linux server, or Kubernetes.

    bliss --target box            # remember a default target
    bliss run -- make test        # run a command there (no command: a shell)
    bliss ai "fix the tests"      # run an AI agent there (no prompt: interactive)
    bliss list                    # what is running, with live state (alias: ls)
    bliss attach ott              # alias: a
    bliss logs | status | pull | stop [name]
    bliss gc [-n]                 # remove cached systems and agents nothing uses

Workloads get names like `sunny-otter`. Commands accept the full name or the
start of either word (`sun`, `ott`). With no name, or `.`, they use the newest
workload from this directory.

## Settings

Targets live in `~/.config/bliss/config.toml` (or `$BLISS_CONFIG`); see
`config.example.toml`. `bliss --target NAME` alone sets the default target.

## What travels

- **Environment:** everything except session variables such as `DISPLAY` and
  `SSH_AUTH_SOCK`, and host-only paths. This includes secrets, so use `[env]
  exclude`. `bliss run -n` lists the names that would be sent.
- **Files:** this directory, minus `.blissignore` (gitignore-style, no negation).
  Files are baked into images, so a Kubernetes target's registry can read them.
- **Logins do not travel.** Only env vars do (e.g. `ANTHROPIC_API_KEY`); login
  files such as `~/.claude/.credentials.json` are not copied.

## Agents

`claude`, `autolith`, `hermes` and `pi`, chosen with `--agent` or `[ai] agent`.

`--unattended` passes the agent's no-approval flag. In containers and pods bliss
also sets `IS_SANDBOX=1` so Claude accepts it as root. Over ssh it does not, so
Claude refuses when the remote user is root. Pi never asks for approval, so it
acts unattended with or without the flag. Autolith's prompt mode goes through a
wrapper, so the flag is not applied there and bliss warns.

Autolith builds its images on first run in each new container, which takes minutes.

## Limits

- x86_64 only for agents; ssh targets must match this machine's architecture.
- Nix ssh targets need Nix on the remote.
- `bliss logs -f` is not supported on ssh targets.
- `bliss gc` does not touch registries or remote Nix stores.
