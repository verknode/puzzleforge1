# Local-first GPU mode

Local-first mode turns one owned GPU into a durable campaign without running
the HTTP coordinator or copying tuning flags by hand. Cloud capacity remains
optional.

## First setup

Install PuzzleForge and place `cuBitCrack`/`clBitCrack` in the current folder,
in `.puzzleforge/bin`, or on `PATH`. An explicit path also works:

```bash
puzzleforge local-setup --binary ./cuBitCrack --puzzle 71
```

On Windows PowerShell:

```powershell
puzzleforge local-setup --binary .\cuBitCrack.exe --puzzle 71
```

Setup performs these gates in order:

1. list the device through the installed engine;
2. solve and independently verify reviewed puzzle #8;
3. benchmark identical control work with several tuning profiles;
4. select the fastest stable profile;
5. derive a durable chunk size from the measured rate;
6. create `.puzzleforge/local/profile.json`, `benchmark.json`, and
   `campaign.sqlite3`.

If any gate fails, the real campaign does not start.

## Run and resume

```bash
puzzleforge local-run
```

Ctrl+C safely stops the current run. A fully completed chunk stays credited;
an unfinished chunk returns to the retry queue. Running the same command later
continues from the SQLite state.

To run a bounded session:

```bash
puzzleforge local-run --max-chunks 3
```

Inspect exact progress and the active measured tuning:

```bash
puzzleforge local-status
```

## Tuning controls

The default quick benchmark is intended for first use:

```bash
puzzleforge local-setup --benchmark-profile quick
```

For a longer comparison use `balanced` or `full`. `--chunk-seconds` controls
the target duration of each durable unit; the default is 300 seconds. Larger
chunks reduce process/checkpoint overhead, while smaller chunks lose less work
after a power cut or driver reset.

The benchmark report records every tested profile, median rate, spread, device
probe, and recommended settings. Campaign coverage is not credited during
benchmarking because the control range is intentionally repeated.

## Multiple owned GPUs

Version 0.6 focuses on a reliable single-device path. Multiple cards can still
use the existing coordinator/worker commands. Native local multi-GPU launch,
per-device thermal limits, and a lightweight dashboard are the next local-first
milestones.
