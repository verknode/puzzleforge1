from __future__ import annotations

from .model import Puzzle


# Address/status snapshot reviewed on 2026-08-24. A future status refresh must
# verify the on-chain target before changing this registry.
_PUZZLES = {
    71: Puzzle(
        number=71,
        start=1 << 70,
        end=(1 << 71) - 1,
        address="1PWo3JeB9jrGwfHDNpdGK54CRas7fsVzXU",
    ),
    72: Puzzle(
        number=72,
        start=1 << 71,
        end=(1 << 72) - 1,
        address="1JTK7s9YVYywfm5XUH7RNhHJH1LshCaRFR",
    ),
    73: Puzzle(
        number=73,
        start=1 << 72,
        end=(1 << 73) - 1,
        address="12VVRNPi4SJqUTsp6FmqDqY5sGosDtysn4",
    ),
    74: Puzzle(
        number=74,
        start=1 << 73,
        end=(1 << 74) - 1,
        address="1FWGcVDK3JGzCC3WtkYetULPszMaK2Jksv",
    ),
}


def puzzles() -> tuple[Puzzle, ...]:
    return tuple(_PUZZLES[number] for number in sorted(_PUZZLES))


def get_puzzle(number: int) -> Puzzle:
    try:
        return _PUZZLES[number]
    except KeyError as exc:
        allowed = ", ".join(str(value) for value in sorted(_PUZZLES))
        raise ValueError(
            f"puzzle #{number} is not in the reviewed registry (allowed: {allowed})"
        ) from exc

