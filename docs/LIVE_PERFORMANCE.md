# Live performance and retuning

This update preserves the campaign database, chunk grid, planner seed and sweep
configuration. It does not change the private shuffle or add a new GPU kernel.

## Dashboard measurements

- **Live engine speed** is the latest rate emitted by BitCrack, not the saved
  benchmark. It becomes unavailable when the report is older than 15 seconds,
  the worker stops or the worker heartbeat expires. Some binaries buffer output;
  an unavailable rate alone does not prove that scanning stopped.
- **Confirmed unique throughput** counts only ranges accepted by the coordinator
  in this session, divided by session wall time. Planning, initialization, failed
  attempts and cooling are included. It jumps at chunk completion, especially
  early in a session. It is neither a moving instantaneous speed nor partial credit.
- **Last chunk** includes initialization and thermal retries in elapsed time.
- **Time to first report** includes initialization, the reporting interval and
  any output buffering. It is not a measurement of initialization alone.
- Worker phases distinguish planning, initializing, scanning, cooling and
  stopped/stale. The dashboard no longer exposes the shuffle seed or found key.

The main BitCrack adapter still starts a new executable for each leased chunk.
Keeping CUDA state alive across noncontiguous chunks needs support in that
external executable. No GPU speedup factor is claimed without a hardware run.

## Correct completion and recovery

The adapter drains stdout and stderr concurrently, keeps a bounded log tail, and
verifies candidates even if later logs scroll them out. A zero process exit alone
does not earn coverage: normal completion requires BitCrack's
`Reached end of keyspace` message. This is the marker in upstream
[KeyFinderLib](https://github.com/brichard19/BitCrack/blob/master/KeyFinderLib/KeyFinder.cpp).
A fork using a different completion marker will fail without credit; its completion
contract must be inspected before adding that marker. A found key is independently
verified against the registered puzzle and accepted only inside the leased range.

Runtime data is observational; SQLite remains the authority for coverage. A local
OS lock prevents two updated local workers or a worker and retune from using the
same campaign concurrently. Remote workers remain governed by coordinator leases.

## Retune an existing campaign

Stop the local worker, then run `Tune-PuzzleForge.cmd`. It measures the current
settings and the quick tuning presets, with one unscored warmup and three scored
trials per setting. Profiles are interleaved in alternating order to reduce bias
from a GPU warming up over time. Each profile is also checked against solved #8.
Thermal protection remains enabled. Test ranges are separate from the coordinator
and do not earn unique coverage.

The initial control range targets approximately 30 seconds at the stored rate;
actual trials can take longer. Settings with failed trials or more than 20% spread
cannot win. A switch needs at least 5% median improvement and a candidate's slowest
sample faster than the baseline's fastest sample. Otherwise current flags stay in
place. No stable baseline means only a report is saved. Before an applied update,
the profile is backed up beside the timestamped report. Retuning never resizes the
campaign's chunks or clears its database. A match stops retuning and is recorded
and processed through the existing verified-match path.

Advanced use: `python -m puzzleforge local-retune --seconds 60 --repeats 3`.
`--presets balanced` and `--presets full` take longer; quick is the default.

## Map and model analysis

Dark green means partly checked; bright green means every key in the displayed
cell is credited. Colors do not rescale with other cells. Select a cell and choose
**Zoom selected cell**; repeat until each cell is a single chunk. Back and Whole
range return to the larger view. Exact positions use integer arithmetic at both
ends of the API, including the shorter final chunk. The map only knows this
campaign's history, not other people's searches.

Model Zoo reuses analysis for identical observations and parameters within the
same process. Modified observations or calibration options get a fresh analysis;
restarts also recompute it. Callers get independent copies of report parameters.
The displayed cadence is one CPU analysis per configured number of GPU chunks,
not a percentage of GPU time. Generator Lab retains the user's enabled/duty
settings instead of being forcibly re-enabled at every launch.

## Updating a Windows ZIP checkout

`scripts/Update-PuzzleForge.ps1 -Revision <40-character-commit> -RepoRoot <folder>`
downloads a pinned source archive and backs up overwritten code under
`.puzzleforge/updates`. It excludes campaign state, executables and virtualenv,
refuses an active local worker, restores copied files on update failure, and
starts the normal launcher. `-Tune` runs the optional sustained retune first.
Existing arbitrary source edits are backed up, not merged; use git for development
checkouts. Python files are copied as bytes to preserve UTF-8.
