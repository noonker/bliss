"""Workload names that are pleasant to type: `sunny-otter`, then `bliss attach ott`."""

from __future__ import annotations

import secrets

_ADJECTIVES = """
amber brave breezy bright calm cheery comfy cosmic cozy crisp daring dizzy dreamy
eager fizzy fuzzy gentle giddy golden happy jolly jazzy kind lively lucky mellow
merry mighty misty nimble peppy plucky quick rosy silly snug sparky spry sunny
swift tidy witty zesty
""".split()

_ANIMALS = """
alpaca badger beaver bison bunny corgi crane dingo dolphin duck ferret finch fox
gecko heron hippo ibis koala lemur llama lynx magpie marmot moose newt ocelot
otter owl panda pony puffin quail robin seal shrew sloth swan tapir toucan turtle
vole walrus wombat yak
""".split()


def new_name(taken: set[str]) -> str:
    """A name not in `taken`. A number only appears once the words run short."""
    for _ in range(100):
        name = f"{secrets.choice(_ADJECTIVES)}-{secrets.choice(_ANIMALS)}"
        if name not in taken:
            return name
    while True:
        name = f"{secrets.choice(_ADJECTIVES)}-{secrets.choice(_ANIMALS)}-{secrets.randbelow(1000)}"
        if name not in taken:
            return name
