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
- [ ] Batched point generation (one scalar multiplication per batch)
- [ ] SIMD SHA-256 and RIPEMD-160 pipeline
- [ ] Stable machine-readable worker protocol
- [ ] Cross-backend golden-vector tests

## M2 — GPU

- [ ] CUDA capability detection and benchmark harness
- [ ] CUDA secp256k1 point-walk kernel
- [ ] Batched HASH160 target comparison on device
- [ ] Multi-GPU scheduling and thermal/power telemetry
- [ ] Checkpoint-safe kernel boundaries

## M3 — distributed search

- [ ] SQLite/PostgreSQL lease coordinator
- [ ] Expiring leases and automatic range recovery
- [ ] Signed worker identity and duplicate-result rejection
- [ ] Live throughput, coverage, ETA, and failure dashboard
- [ ] Export/import of independently audited coverage maps

## Merge rules

1. Never add arbitrary target addresses or ranges.
2. Never merge an optimization that fails golden vectors.
3. Report measured hardware, settings, key rate, and power draw.
4. Keep checkpoints forward-compatible and atomic.
5. Prefer exact coverage accounting over speculative key-selection patterns.

