# Cold Zone scheduling

Cold Zone orders campaign work toward the parts of a puzzle interval that other
public searchers are least likely to have already covered.

## What it does and does not claim

The chunk *set* is unchanged. Every ordering PuzzleForge ships is a bijection
over the same chunks, so for a given number of unique keys the coverage
probability is identical. Cold Zone is not a cryptographic shortcut and does
not change the exact coverage accounting.

Two effects are nevertheless real:

1. **Less duplicated effort.** Public searchers concentrate on a small number of
   obvious regions. Work placed away from those regions is much less likely to
   repeat a range somebody else is running right now.
2. **A genuine posterior tilt.** A region other people already searched without
   solving the puzzle is *less* likely to hold the key. Conditioning on "the
   puzzle is still unsolved" shifts probability away from heavily searched
   regions.

The size of the second effect is bounded by how much of the interval the public
has actually covered. For puzzle #71 that fraction is negligible, so the honest
claim is the first effect. The default preset keeps an unbiased lane running
alongside the cold lane for exactly this reason.

## The density model

`src/puzzleforge/coldzone.py` splits the normalized interval `[0, 1)` into
bands (4096 by default) and scores each band with a weighted sum of six
components. Each component is normalized to a mean of one, so the weights are
shares of modelled effort and the combined density also has a mean of one.

| Component | Weight | Why searchers go there |
|---|---|---|
| `sequential-low` | 0.34 | Most operators start at the interval start and walk upward |
| `sequential-high` | 0.08 | A smaller group walks downward from the interval end |
| `round-boundary` | 0.10 | Hand-picked ranges cluster on hex-round boundaries |
| `center-split` | 0.05 | Splitting the interval in half is a common first guess |
| `solved-echo` | 0.28 | Pattern searchers aim at the normalized positions of solved puzzles |
| `uniform-floor` | 0.15 | Random-mode searchers spread thinly and evenly |

This is a **behavioural prior**, not measured telemetry from other searchers.
Unlike Hypothesis Lab, it cannot be validated against the solved vectors,
because there is no public dataset of who searched which range. Treat the
weights as documented assumptions that can be argued with, not as measurements.

## The work order

`ColdOrder` sorts bands cold-to-hot, then walks them in that order. Inside a
band, chunks are visited through the same keyed format-preserving permutation
the uniform lane uses, seeded per band, so two installations with different
seeds do not collide even when they agree on which band is coldest.

The result is a bijection over the whole chunk domain: every chunk is still
emitted exactly once, and the SQLite global duplicate filter still applies.

## Using it

Preview the coldest bands and the resulting work order:

```bash
puzzleforge cold-preview 71 --chunk-size 0x100000000 --preview 16
```

The report names each band's key interval, its modelled relative effort, and
which component dominates that score.

Start a local campaign in cold mode:

```bash
puzzleforge local-setup --binary ./cuBitCrack --puzzle 71 --mode cold
puzzleforge local-app
```

Or a distributed campaign:

```bash
puzzleforge coordinator-init campaign.sqlite3 71 \
  --chunk-size 0x100000000 \
  --mode cold
```

## Lane mix

The `cold` preset is a MOSAIC lane set:

| Lane | Weight | Role |
|---|---|---|
| `cold` | 6 | Coldest bands first |
| `uniform` | 4 | Unbiased private permutation |

Six of every ten chunks go to the least-searched bands and four keep unbiased
coverage running. `coordinator-status` and the dashboard report this campaign
as `cold`; the stored planner mode is still `mosaic`, so no schema change is
needed and existing campaigns keep their own lane set across restarts and
reseeds.
