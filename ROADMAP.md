# Roadmap

## M0 — correctness foundation

- [x] Reviewed puzzle registry for #71–#74
- [x] Pure-Python secp256k1/P2PKH oracle
- [x] Bijective chunk permutation and multi-machine sharding
- [x] Parallel reference scan and atomic resume checkpoint
- [x] Runtime/probability estimator
- [x] Unit tests and CI

## M1 — native engine

- [ ] C++20 reference backend using libsecp256k1
- [x] Batched point generation (one scalar multiplication per batch)
- [ ] SIMD SHA-256 and RIPEMD-160 pipeline
- [x] Stable machine-readable worker protocol
- [ ] Cross-backend golden-vector tests

## M2 — GPU

- [x] cuBitCrack/clBitCrack capability probe and strict execution adapter
- [x] Reproducible GPU benchmark and auto-tuning harness
- [x] Local-first setup, adaptive checkpoint chunks, and one-command resume
- [ ] CUDA secp256k1 point-walk kernel
- [ ] Batched HASH160 target comparison on device
- [ ] Multi-GPU scheduling and thermal/power telemetry
- [ ] Checkpoint-safe kernel boundaries
- [x] Automatic temperature guard with abort, cooldown, and same-chunk retry
- [ ] Adaptive power/throttling optimization from long-session telemetry
- [x] Lightweight mobile-friendly local web dashboard
- [x] Combined local app command and Windows double-click launcher
- [ ] Signed standalone desktop executable and dashboard start/stop controls

## M3 — distributed search

- [x] SQLite transactional lease coordinator
- [x] Expiring leases and automatic range recovery
- [x] Bearer-authenticated workers and duplicate-result rejection
- [x] Exact unique-coverage vs random-with-replacement metrics
- [ ] Live throughput, coverage, ETA, and failure dashboard
- [ ] Export/import of independently audited coverage maps

## M4 — experimental scheduling and elastic capacity

- [x] MOSAIC deterministic strategy lanes and global de-duplication planner
- [x] Cold Zone public-search-density prior and least-searched-first ordering
- [x] In-place search-mode switching that preserves completed coverage
- [x] Persist MOSAIC lane cursors in the lease coordinator
- [x] Verified solved-vector dataset and forward-only holdout backtests
- [x] Persistent Hypothesis Lab 10/90 analysis/search cycles
- [x] Multiple-testing-adjusted validation labels for measured lift
- [x] Automatic uniform fallback for strategies without measurable lift
- [x] Fingerprinted 126-model registry with 70 gated and 56 shadow models
- [x] Synthetic-uniform maximum-statistic calibration and stability gate
- [x] Read-only Vast.ai and RunPod catalog adapters
- [x] Measured price/performance qualification and dry-run selection
- [x] Hard hourly/daily budget policy before any provisioning
- [ ] Explicit two-phase paid provisioning and automatic termination
- [ ] Spot/preemption recovery through the existing lease protocol

## Merge rules

1. Never add arbitrary target addresses or ranges.
2. Never merge an optimization that fails golden vectors.
3. Report measured hardware, settings, key rate, and power draw.
4. Keep checkpoints forward-compatible and atomic.
5. Prefer exact coverage accounting over speculative key-selection patterns.
