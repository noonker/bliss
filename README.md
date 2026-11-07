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

For other tools, `run --json` and `ai --json` print the started workload
(`{"name", "target"}`) on stdout, and `status --json` prints its state with `finished`
(the command has ended, even if a shell is kept open after it) and `exit`.

Workloads get names like `sunny-otter`. Commands accept the full name or the
start of either word (`sun`, `ott`). With no name, or `.`, they use the newest
workload from this directory.

## Running here

`bliss -t here ...` has the same commands, on this machine and in place. It needs no
settings, and `bliss -t here` makes it the default.

- **Nothing is packed or copied.** The workload runs in your real directory with your real
  environment, and changes happen there. `.blissignore` and `[env]` have no effect.
- **`stop` does not touch your directory.** It ends the session and removes bliss's own
  state. `pull` has nothing to do.
- **Sessions are on a private tmux server,** so your own are safe; see them with
  `tmux -L bliss ls`. Attaching from inside tmux nests, so the detach key is `C-b C-b d`.
  It reads your `~/.tmux.conf`, so an error in that file appears in copy mode on the
  first attach after the server starts; press `q`.
- **Agents are whatever is on your `PATH`.** bliss downloads nothing and sends no
  `credentials`; the agent uses the login already here.
- **There is no sandbox.** `--unattended` lets an agent act directly on this machine.

## Settings

Targets live in `~/.config/bliss/config.toml` (or `$BLISS_CONFIG`); see
`config.example.toml`. `bliss --target NAME` alone sets the default target.

## What travels

- **Environment:** everything except session variables such as `DISPLAY` and
  `SSH_AUTH_SOCK`, and host-only paths. This includes secrets, so use `[env]
  exclude`. `bliss run -n` lists the names that would be sent.
- **Files:** this directory, minus `.blissignore` (gitignore-style, no negation).
  Files are baked into images, so a Kubernetes target's registry can read them.
- **Logins do not travel unless you ask.** Env vars do (e.g. `ANTHROPIC_API_KEY`);
  login files only with `credentials` under `[agents.<name>]`. See below.

## Kubernetes

A target needs a `registry` to push to. If the cluster reaches the same registry at
another address, such as an in-cluster service, set that as `pull_registry`.

## Sending a login

    [agents.claude]
    credentials = ["~/.claude/.credentials.json"]

This works for `claude` and `pi`, the agents whose config directory can be moved. Each
workload gets its own private copy (`CLAUDE_CONFIG_DIR`, `PI_CODING_AGENT_DIR`), so an
existing login on the target is not overwritten, and `bliss stop` removes it. It is never
baked into an image. `bliss ai -n` shows what would be sent.

Weigh this before turning it on:

- This is your whole account, not a scoped key. Anyone with root on the target, or who
  can read Secrets in the Kubernetes namespace, can use it. An API key in the
  environment is easier to limit and revoke.
- The copy on the target can refresh its own tokens, which may sign out your local
  login or leave the two out of step.
- The rest of your config, such as settings and memory, does not travel.

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
- Hermes installs in place and builds a web UI, so ssh targets with under 4 GB of RAM
  are refused: on a 1 GB host it ran the machine out of memory and the kernel killed
  other processes. `min_install_memory_mb = 0` under `[agents.hermes]` overrides this.
- `kubectl attach` ignores the `C-p C-q` detach keys; end it from outside, or close
  the terminal. The workload keeps running.
- `bliss logs -f` is not supported on ssh targets.
- `bliss gc` does not touch registries or remote Nix stores.
