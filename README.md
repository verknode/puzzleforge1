# PuzzleForge

PuzzleForge is a challenge-scoped compute and coordination toolkit for the
public Bitcoin Puzzle Transaction. It targets the still-open address-only
puzzles #71 through #74 and includes solved puzzle #8 as an end-to-end test.

The project deliberately accepts only puzzles in its reviewed registry. It is
not a general wallet scanner, seed finder, recovery service, or general-purpose
transaction broadcaster. Its only transaction feature is an opt-in local sweep
of a key independently verified against a registered public-puzzle address.

## What works today

- dependency-free secp256k1 and compressed P2PKH reference implementation;
- reviewed registry entries for puzzles #8 and #71–#74;
- deterministic, non-overlapping, pseudo-random chunk allocation;
- parallel CPU reference scanner with resumable atomic checkpoints;
- strict cuBitCrack/clBitCrack adapter with independent result verification;
- local-first GPU profile with validation, auto-tuning, adaptive durable chunks,
  and one-command resume;
- lightweight mobile-friendly dashboard with cached NVIDIA load, temperature,
  power, memory, speed, exact coverage telemetry, and a tappable coarse
  keyspace map backed by the durable chunk database;
- temperature guard with process abort, cooldown hysteresis, and same-chunk retry;
- combined local app command and Windows double-click launcher;
- Hypothesis Lab with a 126-model fingerprinted zoo, verified solved vectors,
  forward-only scoring, empirical uniform-null calibration,
  persistent 10/90 research-to-search cycles, and global range de-duplication;
- Generator Lab with durable, challenge-scoped seed/generator experiments,
  exact solved-vector holdouts, and independent target-address verification;
- SQLite coordinator with transactional leases and automatic expired-work recovery;
- authenticated HTTP worker protocol for many remote GPU machines;
- exact coverage accounting and a random-with-replacement comparison;
- read-only Vast.ai/RunPod catalogs and a hard-budget cloud capacity planner;
- private-key verification against an official puzzle address;
- opt-in local signing and dual-provider broadcast of a verified puzzle reward;
- probability and runtime estimates that separate experimental ordering from
  proven unique coverage;
- unit tests and GitHub Actions CI.

The Python scanner is a correctness oracle, not the fast path. GPU work is sent
to a locally installed BitCrack binary. PuzzleForge owns target selection,
range allocation, lease recovery, accounting, and result verification.

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

## Local-first mode

The default practical path is one owned GPU. `local-setup` finds BitCrack,
proves the full path against solved puzzle #8, benchmarks tuning profiles,
chooses a checkpoint chunk sized for roughly five minutes, and creates one
durable local campaign:

```bash
puzzleforge local-setup --binary ./cuBitCrack --puzzle 71
puzzleforge local-run
```

Stop with Ctrl+C and start `local-run` again later. Completed chunks are never
repeated, while an interrupted or failed chunk is returned to the retry queue.
The measured flags are loaded automatically; they do not have to be copied
into each command.

After setup, start computation and the dashboard together with one command:

```bash
puzzleforge local-app
```

On Windows, `Start-PuzzleForge.cmd` creates/updates the local environment,
performs first setup when `cuBitCrack.exe` is present, starts the campaign, and
opens the dashboard. It also enables Hypothesis Lab and Generator Lab for both
new and existing local profiles. Subsequent launches resume both durable
campaign cursors.

### Arm a verified-match sweep

Auto-sweep is disabled by default. Configure a Bitcoin mainnet Native SegWit
(`bc1q`) destination only after checking it in the receiving wallet:

```bash
puzzleforge local-sweep-configure bc1qYOUR_CHECKED_ADDRESS
```

The destination and fee bounds are stored only in the local profile. If a key
is found, PuzzleForge first verifies that it derives the registered puzzle
address, obtains confirmed UTXOs from two independent Esplora providers, signs
the sweep locally, saves the signed transaction atomically, and broadcasts it
to both providers. The private key is never submitted to an API. A network
failure leaves the exact signed transaction in `sweep.json` for idempotent
rebroadcast at the next launch; an accepted broadcast clears the plaintext key
from the campaign database. The dashboard reports `ARMED`, `PENDING`, or
`BROADCAST` and shows the transaction ID.

Disable automatic sweeping without deleting the saved destination:

```bash
puzzleforge local-sweep-configure --disable
```

```bash
puzzleforge local-status
```

Run the read-only dashboard in a second terminal:

```bash
puzzleforge local-dashboard
```

To view it from a phone on the same trusted network, bind to the computer's LAN
interface and open port 8788 using the computer's local IP:

```bash
puzzleforge local-dashboard --host 0.0.0.0
```

## Thermal protection

New local profiles use an 82 C hard limit and resume at 72 C. The guard checks
NVIDIA telemetry every three seconds. If the limit is reached, PuzzleForge
terminates the current engine process, credits zero work for that attempt,
waits for cooldown, and retries the exact same chunk. A completed result is
never replaced by a partial result.

Configure thresholds during first setup:

```bash
puzzleforge local-setup --binary ./cuBitCrack --max-temp 80 --resume-temp 70
```

`--no-thermal-guard` is available for a non-NVIDIA engine without
`nvidia-smi`, but removes this protection.

## Hypothesis Lab 10/90 mode

New local campaigns use `--mode hypothesis` by default. The lab validates the
public solved vectors #1-#70, normalizes each solution inside its published
interval, and evaluates 126 parameterized models with forward-only holdouts.
Seventy candidates share an empirical family-wide gate calibrated on synthetic
uniform histories; 56 flexible models remain shadow-only. One research phase
selects a normalized cell, then feeds nine unique ranges to the GPU. After the
ninth range, it analyzes again and repeats:

```bash
puzzleforge hypothesis-preview 71 --preview 18 --top-models 8
puzzleforge hypothesis-enable
puzzleforge local-app
```

The 10/90 ratio means one analysis decision for nine GPU chunks. The analysis
is short CPU work; PuzzleForge does not deliberately idle the GPU for 10% of
wall-clock time. Every proposed chunk still passes through the SQLite global
duplicate filter, so an experimental priority never creates fake coverage.

The current dataset does not pass the adjusted validation gate for a proven
non-uniform lift. The lab therefore selects `UNIFORM FALLBACK`; an experimental
model cannot consume the nine GPU slots until it passes the gate. See
[docs/HYPOTHESIS_LAB.md](docs/HYPOTHESIS_LAB.md) and
[docs/MODEL_ZOO.md](docs/MODEL_ZOO.md).

## Generator Lab

Generator Lab is a separate experiment limited to the registered public
Bitcoin Puzzle challenge. It tests deterministic generator families against
the public solved vectors, including SHA-256/HMAC variants, hash chains,
MT19937, raw BIP32 paths, BIP39/BIP44 derivation, contextual phrases, dates,
and timestamps. A candidate must match the newest control vector exactly,
match five solved holdouts exactly, and then derive the registered target
address before it can stop the campaign.

The Windows launcher enables it automatically. Enable it manually or add an
optional challenge-specific wordlist with:

```bash
puzzleforge generator-enable --cpu-percent 10
puzzleforge generator-enable --cpu-percent 10 --wordlist ./seeds.txt
```

The default duty cycle uses 10% of one CPU core in the background and reserves
0% of the GPU, so BitCrack keeps the GPU. Progress is atomic and resumes from
`generator-lab.json`; the dashboard never exposes seed material or a recovered
private key. Partial bit matches are diagnostic only and do not increase the
reported success probability. See [docs/GENERATOR_LAB.md](docs/GENERATOR_LAB.md).

Use `--benchmark-profile full` for a longer tune or `--chunk-seconds 600` to
reduce checkpoint frequency. See [docs/LOCAL_FIRST.md](docs/LOCAL_FIRST.md).

Validate an installed GPU engine against solved puzzle #8. A correct run finds
the known `0xe0` test value and independently derives the registered address:

```bash
puzzleforge gpu-probe --binary ./cuBitCrack
puzzleforge gpu-test --binary ./cuBitCrack --device 0
```

Auto-tune the engine on the installed GPU. Every benchmark first has to pass
the solved #8 validation, then compares identical #71 work across several
profiles and writes a reproducible JSON report:

```bash
puzzleforge gpu-benchmark \
  --binary ./cuBitCrack \
  --device 0 \
  --profile quick \
  --repeats 2 \
  --output benchmark-rtx4090.json
```

Copy the reported `recommended_flags` into `gpu-worker`. The benchmark does not
credit campaign coverage because each profile intentionally repeats the same
control range.

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

## Distributed GPU campaign

Create a database and start the coordinator on the control machine:

```bash
export PUZZLEFORGE_API_TOKEN="$(puzzleforge token)"
puzzleforge coordinator-init campaign.sqlite3 71 \
  --chunk-size 0x100000000 \
  --seed furnes-gpu-v1
puzzleforge coordinator-serve campaign.sqlite3 --host 127.0.0.1 --port 8787
```

Run a worker on the same machine:

```bash
export PUZZLEFORGE_API_TOKEN="the-same-secret-token"
puzzleforge gpu-worker \
  --coordinator http://127.0.0.1:8787 \
  --binary ./cuBitCrack \
  --worker rtx4090-a \
  --device 0 --blocks 32 --threads 256 --points 1024
```

Remote workers require HTTPS by default. A trusted encrypted tunnel can use
`--allow-insecure-http`. Never put the API token in a command argument or
commit it. See [docs/DISTRIBUTED.md](docs/DISTRIBUTED.md).

Inspect exact progress at any time:

```bash
puzzleforge coordinator-status campaign.sqlite3
```

## What is actually different

PuzzleForge does not claim a new cryptographic shortcut. Existing tools already
provide fast elliptic-curve kernels. Its useful distinction is the audited
campaign layer around those kernels: a bijective work order, no duplicate live
leases, automatic recovery, strict full-range acknowledgements, independently
verified matches, and exact durable coverage.

For the same number of attempts, unique sampling is strictly better than random
sampling with replacement because repeated candidates add no coverage. The
advantage starts tiny and grows with campaign size; higher measured throughput
remains the dominant practical improvement.

## Experimental MOSAIC scheduler

`mosaic-preview` interleaves uniform, bit-spread, edge-in, and center-out orders
behind one global duplicate filter:

```bash
puzzleforge mosaic-preview 71 --chunk-size 0x100000000 --preview 32
puzzleforge coordinator-init campaign.sqlite3 71 \
  --mode mosaic --chunk-size 0x100000000
```

This is research infrastructure, not a claimed shortcut. A different ordering
can help only if a tested non-uniform prior exists. See
[docs/MOSAIC.md](docs/MOSAIC.md).

## Optional elastic cloud planning

Fetch current offers without renting anything, then build a dry-run plan from
measured rates and explicit budgets:

```bash
export VAST_API_KEY="..."
puzzleforge cloud-catalog vast \
  --gpu "RTX 4090" \
  --output vast-offers.json

puzzleforge cloud-plan vast-offers.json \
  --rate "RTX 4090=5000000000" \
  --max-instances 4 \
  --max-total-hourly 2.00 \
  --max-daily 20.00 \
  --max-offer-hourly 0.70 \
  --max-cost-per-quadrillion 40.00
```

The output always contains `"dry_run": true`; the current release cannot create
a paid instance. See [docs/ELASTIC_SWARM.md](docs/ELASTIC_SWARM.md).

## Honest scale

For an address-only puzzle, testing a unique fraction `f` of the range gives
exactly `f` probability of success, assuming the unknown value is uniformly
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
