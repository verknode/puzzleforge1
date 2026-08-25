# Model Zoo

Model Zoo is the research engine inside Hypothesis Lab. Version 1 evaluates
126 parameterized range-ordering models against the same verified public
solution history. It is a model-selection system, not evidence that the next
challenge value is predictable.

## Registry

The registry is immutable and fingerprinted. A report records the 16-character
SHA-256 fingerprint, so results from different model sets are not silently
mixed.

| Family | Models | Promotion eligible | Shadow only |
| --- | ---: | ---: | ---: |
| Histogram | 6 | 3 | 3 |
| Kernel density | 8 | 8 | 0 |
| Recent-window KDE | 15 | 15 | 0 |
| Lag KDE | 10 | 10 | 0 |
| Mean/median delta | 16 | 16 | 0 |
| Beta distribution | 3 | 3 | 0 |
| Autoregression | 6 | 4 | 2 |
| Modular residues | 7 | 3 | 4 |
| Bit weight, runs, independence, Markov | 12 | 8 | 4 |
| XOR-mode predictors | 9 | 0 | 9 |
| Spectral predictors | 16 | 0 | 16 |
| LCG, XorShift, hash-chain fingerprints | 18 | 0 | 18 |
| **Total** | **126** | **70** | **56** |

Wide histograms, high-resolution bit models, spectral sweeps, and generator
fingerprints are deliberately shadow-only. They are useful for measurement,
but their flexibility makes accidental historical fits especially easy.
Shadow models never choose a GPU range, regardless of their displayed score.

## Evidence gate

Every eligible model is tested forward in time. For each held-out puzzle after
the first 16 observations, training contains only earlier solutions. The score
is mean log density relative to a uniform density of one.

The complete eligible registry is then calibrated together against 128
deterministic synthetic uniform histories of the same length. Each synthetic
run records the best score across all 70 eligible models. An observed model can
be promoted only when:

1. its mean log lift is positive;
2. both the early and late holdout halves are positive;
3. its empirical p-value against the synthetic *maximum* is at most 0.05.

The maximum-statistic comparison accounts for choosing the winner from many
models. Per-model empirical p-values and Benjamini-Hochberg q-values are also
reported for audit, but promotion uses the stricter family-wide maximum gate.

The current verified #1-#70 history promotes zero models. PuzzleForge therefore
keeps the seeded uniform permutation. This is the expected safe result when a
large search finds no reproducible evidence.

## Bounded reports and reproducibility

The planner stores counts, the calibration size, the null 95th percentile,
the best eligible and shadow candidates, and at most 20 detailed scores. The
CLI can show fewer:

```bash
puzzleforge hypothesis-preview 71 --preview 18 --top-models 8
```

The first analysis after process start performs the synthetic calibration.
Later 10/90 cycles reuse that immutable calibration in memory and only rescore
the verified observations. Existing v0.9 campaign state remains readable; any
already queued ranges are finished before the next Model Zoo cycle is built.

## What would count as progress

A higher historical score alone is not progress. A useful new model must pass
the planted-signal test, the no-leakage test, uniform-null calibration, both
forward eras, deterministic restoration, and the global duplicate-range
filter. Even a promoted result is a measured historical ordering advantage,
not a guarantee for an unsolved challenge.
