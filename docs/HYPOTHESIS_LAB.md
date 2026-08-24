# Hypothesis Lab

Hypothesis Lab is an experimental range-ordering layer. It does not alter the
cryptographic test performed by BitCrack and does not claim that the public
challenge keys contain a predictable pattern.

## 10/90 cycle

Each durable cycle contains:

1. one CPU research phase (10% of the cycle structure);
2. nine unique GPU search chunks selected by that report (90%);
3. a new research phase after the ninth chunk.

The analysis normally finishes far faster than a GPU chunk, so the program does
not sleep merely to consume 10% of wall-clock time. Failed or interrupted GPU
work returns to the existing retry queue. Completed, leased, and queued chunks
all participate in the same SQLite duplicate filter.

## Dataset integrity

The bundled observations contain public solved challenge vectors #1-#70 from
`roadhero/Bitcoin-Puzzle-Info` commit
`6bbd33dcefe2b4d039f96f437e86ea5f918de495`. Before analysis, PuzzleForge:

- checks that puzzle numbers are consecutive;
- checks that every value lies in its published power-of-two interval;
- independently derives every compressed P2PKH address;
- refuses to schedule from the dataset if any vector fails.

## Models and evidence gate

The fixed model set contains 8-bin and 16-bin histograms, two kernel-density
bandwidths, a recent-window density, a previous-position model, and a recent
delta model. For each historical target after the first 16 observations, the
model is trained only on earlier puzzles. The held-out value is never included
in its own prediction.

The report compares geometric density lift against a uniform density, computes
a one-sided score, and applies a multiple-model adjustment. A model is labelled
`validated` only when its adjusted value is below 0.05 and its forward log lift
is positive.

With the currently bundled #1-#70 dataset, no tested model passes that gate.
The planner therefore rejects the speculative models and uses a seeded uniform
permutation for the next nine chunks. The dashboard labels this `UNIFORM
FALLBACK`. A hypothesis receives priority only after passing the gate.

## Range selection

The target interval is divided into 256 normalized cells. The selected model
ranks their midpoints. Inside the best available cell, a seeded affine
permutation produces nine chunk IDs without enumerating the entire interval.
Cell cursors, queued chunks, the current cycle, and the full report are stored
transactionally in `campaign.sqlite3`.

Preview without starting a GPU process:

```bash
puzzleforge hypothesis-preview 71 --chunk-size 0x10000000000 --preview 18
```

Enable it on an existing local campaign without losing previous coverage:

```bash
puzzleforge hypothesis-enable
```

The Windows launcher runs this upgrade command automatically.
