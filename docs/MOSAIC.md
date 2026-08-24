# MOSAIC experimental scheduler

MOSAIC interleaves several complete, deterministic chunk permutations behind
one global duplicate filter:

- `uniform`: a seeded affine permutation;
- `spread`: a seeded bit-spread permutation with cycle walking;
- `edges`: alternates from the low and high ends;
- `center`: alternates outward from the interval center.

Every lane visits every chunk exactly once. The global filter makes the merged
stream unique even when lanes propose the same chunk.

```bash
puzzleforge mosaic-preview 71 \
  --chunk-size 0x100000000 \
  --seed experiment-v1 \
  --preview 32
```

Create a durable distributed campaign in MOSAIC mode:

```bash
puzzleforge coordinator-init campaign.sqlite3 71 \
  --mode mosaic \
  --chunk-size 0x100000000 \
  --seed experiment-v1
```

Lane cursors and the merged schedule are committed in the same SQLite
transaction that creates each work item. Reopening the coordinator therefore
continues the exact stream. Databases created by schema v1 are migrated to the
standard affine mode without changing their allocated work.

## Claim boundary

For a uniformly distributed target, `N` unique attempts have the same success
probability regardless of order. MOSAIC does not change that fact. It can only
help early when a non-uniform prior is real.

No strategy lane should receive production capacity until it beats a uniform
control on held-out solved examples with a predeclared metric. Failed or
inconclusive strategies must remain disabled. Live throughput, temperature, or
price can tune hardware allocation; a partial address resemblance is not a
learning signal.

## Research still required

The current module implements deterministic orders, transactional persisted
state, global de-duplication, distributed leases, tests, and CLI previews. The
next milestone adds holdout datasets and significance checks. Until a lane
demonstrates measurable lift, MOSAIC remains an experimental ordering rather
than a probability claim.
