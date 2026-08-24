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

The default local run is protected by a hysteresis guard: it aborts an
uncredited in-progress engine process at 82 C, waits until 72 C, then retries
the same durable chunk. Repeated telemetry loss also stops the attempt rather
than silently claiming coverage.

To run a bounded session:

```bash
puzzleforge local-run --max-chunks 3
```

Inspect exact progress and the active measured tuning:

```bash
puzzleforge local-status
```

## Visual dashboard

The dashboard is a separate read-only process, so rendering and browser refresh
do not share the GPU work loop:

```bash
puzzleforge local-dashboard
```

It displays measured speed, exact unique coverage, chunk/failure counters, and
cached `nvidia-smi` telemetry for load, temperature, power, clock, and memory.
The browser polls every three seconds and the GPU telemetry command is cached.

For a phone on the same trusted LAN:

```bash
puzzleforge local-dashboard --host 0.0.0.0 --port 8788
```

Open `http://COMPUTER_LOCAL_IP:8788` on the phone. The dashboard has no mutation
endpoints, but binding beyond localhost exposes campaign statistics to that
network, so it should not be forwarded directly to the public internet.

To launch the worker and dashboard together:

```bash
puzzleforge local-app
```

On Windows, double-click `Start-PuzzleForge.cmd`. The launcher creates a local
virtual environment, installs the checked-out PuzzleForge version, detects a
`cuBitCrack.exe`/`clBitCrack.exe` placed in the repository or
`.puzzleforge/bin`, performs setup once, and resumes on later launches.

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

Version 0.8 focuses on a reliable protected single-device path. Multiple cards
can still use the existing coordinator/worker commands. Native local multi-GPU
launch, per-device supervision, and adaptive power tuning are the next
local-first milestones.
