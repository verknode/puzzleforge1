# Distributed operation

## Invariants

The coordinator enforces these rules inside SQLite transactions:

1. Only a target compiled into the reviewed registry can initialize a campaign.
2. Chunk order is a seeded bijection, so every chunk appears exactly once.
3. At most one unexpired lease exists for a work item.
4. A no-match result is credited only when it covers the entire leased range.
5. A reported match must be inside its lease and independently derive the
   campaign address.
6. Expired and failed work returns to the retry queue without adding coverage.
7. Repeated completion requests with the same token and result are idempotent.

The database stores large aggregate counters as decimal text because puzzle
ranges exceed SQLite's signed 64-bit integer limit. Chunk identifiers remain
integers; initialization rejects a chunk size that would create more than
`2^63-1` chunks.

## Coordinator

```bash
python -m pip install -e .
export PUZZLEFORGE_API_TOKEN="$(puzzleforge token)"
puzzleforge coordinator-init campaign.sqlite3 71 \
  --chunk-size 0x100000000 \
  --seed campaign-v1
puzzleforge coordinator-serve campaign.sqlite3 --host 127.0.0.1 --port 8787
```

Back up `campaign.sqlite3` together with its `-wal` file while the server is
running, or stop the server before copying only the main file.

## Worker

Install and test cuBitCrack or clBitCrack first:

```bash
puzzleforge gpu-probe --binary ./cuBitCrack
puzzleforge gpu-test --binary ./cuBitCrack --device 0
```

Then start the long-running worker:

```bash
export PUZZLEFORGE_API_TOKEN="copied-through-a-secret-channel"
puzzleforge gpu-worker \
  --coordinator https://coordinator.example \
  --binary ./cuBitCrack \
  --worker host-a-gpu-0 \
  --device 0 \
  --lease-seconds 900
```

PuzzleForge invokes the engine without a shell and supplies the target and
exact range itself. A worker cannot request an arbitrary target through the
protocol.

To use the experimental multi-order planner, add `--mode mosaic` when creating
the database. Planner state and accepted work are updated atomically. Existing
schema-v1 databases automatically migrate to `affine` mode when opened.

## HTTP API

All `/v1/*` routes require `Authorization: Bearer ...`. `/health` is public.

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness check |
| `GET` | `/v1/status` | Campaign state and exact coverage |
| `POST` | `/v1/lease` | Atomically obtain one unique chunk |
| `POST` | `/v1/heartbeat` | Extend an active lease |
| `POST` | `/v1/complete` | Submit a full no-match or verified match |
| `POST` | `/v1/fail` | Return failed work to the retry queue |

The built-in server is intentionally small. Expose it remotely only behind TLS
or through an encrypted private network. Rotate the environment token after any
suspected leak.

## Recovery behavior

- Process exit during engine work: heartbeat stops; the lease expires and is
  reassigned.
- Engine timeout or non-zero exit: the worker reports failure; zero keys are
  credited.
- Lost completion response: the worker may submit the identical completion
  again; the coordinator treats it as idempotent.
- Late result after expiry: rejected, preventing stale work from being counted
  after reassignment.
