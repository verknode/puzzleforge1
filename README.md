# PuzzleForge

PuzzleForge is a challenge-scoped compute and coordination toolkit for the
public Bitcoin Puzzle Transaction. It targets the still-open address-only
puzzles #71 through #74 and includes solved puzzle #8 as an end-to-end test.

The project deliberately accepts only puzzles in its reviewed registry. It is
not a general wallet scanner, seed finder, recovery service, or transaction
broadcaster.

## What works today

- dependency-free secp256k1 and compressed P2PKH reference implementation;
- reviewed registry entries for puzzles #8 and #71–#74;
- deterministic, non-overlapping, pseudo-random chunk allocation;
- parallel CPU reference scanner with resumable atomic checkpoints;
- strict cuBitCrack/clBitCrack adapter with independent result verification;
- local-first GPU profile with validation, auto-tuning, adaptive durable chunks,
  and one-command resume;
- lightweight mobile-friendly dashboard with cached NVIDIA load, temperature,
  power, memory, speed, and exact coverage telemetry;
- SQLite coordinator with transactional leases and automatic expired-work recovery;
- authenticated HTTP worker protocol for many remote GPU machines;
- exact coverage accounting and a random-with-replacement comparison;
- read-only Vast.ai/RunPod catalogs and a hard-budget cloud capacity planner;
- private-key verification against an official puzzle address;
- probability and runtime estimates with no fake "AI pattern" claims;
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

The output always contains `"dry_run": true`; version 0.5 cannot create a paid
instance. See [docs/ELASTIC_SWARM.md](docs/ELASTIC_SWARM.md).

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
