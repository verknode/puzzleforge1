# PuzzleForge

PuzzleForge is a challenge-scoped research toolkit for the public Bitcoin
Puzzle Transaction. The first milestone targets the still-open address-only
puzzles #71 through #74.

The project deliberately accepts only puzzles in its reviewed registry. It is
not a general wallet scanner, seed finder, recovery service, or transaction
broadcaster.

## What works today

- dependency-free secp256k1 and compressed P2PKH reference implementation;
- verified registry entries for puzzles #71–#74;
- deterministic, non-overlapping, pseudo-random chunk allocation;
- parallel CPU reference scanner with resumable atomic checkpoints;
- private-key verification against an official puzzle address;
- probability and runtime estimates with no fake "AI pattern" claims;
- unit tests and GitHub Actions CI.

The Python scanner is a correctness oracle and orchestration foundation, not a
competitive GPU engine. Puzzle #71 alone contains `2^70` candidates. A serious
attempt needs a native GPU backend and many coordinated devices.

## Quick start

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .

puzzleforge list
puzzleforge inspect 71
puzzleforge plan 71 --chunk-size 0x100000 --seed furnes
puzzleforge estimate 71 --rate 1500000000
```

Run one small reference chunk and save progress:

```bash
puzzleforge scan 71 \
  --chunk-size 10000 \
  --chunks 1 \
  --workers 4 \
  --seed furnes
```

Resume by running the same command again. The checkpoint records the next
chunk. Different machines must use the same seed and chunk size, a shared
`--shards` value, and a unique `--shard-index`:

```bash
# machine 0
puzzleforge scan 71 --shards 4 --shard-index 0 --seed team-a

# machine 1
puzzleforge scan 71 --shards 4 --shard-index 1 --seed team-a
```

## Honest scale

For an address-only puzzle, testing a fraction `f` of the range gives exactly
`f` probability of finding the key, assuming the unknown key is uniformly
distributed. There is no known shortcut for #71–#74. PuzzleForge therefore
prioritizes fast kernels, zero duplicated work, verifiable checkpoints, and
measured throughput.

## Safety boundary

Only the public challenge targets compiled into `registry.py` are accepted.
Arbitrary addresses and arbitrary private-key ranges are intentionally not CLI
features. See [SECURITY.md](SECURITY.md).

## Roadmap

See [ROADMAP.md](ROADMAP.md). Every performance claim must be backed by a
reproducible benchmark and every optimization must match the Python reference
vectors before it is merged.

