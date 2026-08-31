# Generator Lab

Generator Lab is a challenge-scoped experiment for addresses in PuzzleForge's
reviewed public Bitcoin Puzzle registry. It is not a general wallet scanner or
wallet-recovery feature.

## What it tests

The default experiment combines public-context phrases, calendar strings, and
timestamps with deterministic generator families:

- SHA-256 seed/index order and index-origin variants;
- HMAC-SHA256 variants;
- SHA-256 hash chains;
- Python MT19937 output;
- raw BIP32 normal and hardened paths;
- BIP39 seed derivation followed by a BIP44 external path.

An optional text wordlist can be supplied explicitly. Blank, duplicate, and
overlong entries are ignored; the file is limited to 64 MiB.

## Evidence gate

For each seed and generator pair, the newest public solved vector is an exact
filter. A filter match must also reproduce the five most recent solved
holdouts exactly. Only then is the target key derived, masked into its
published puzzle interval, and independently checked against the registered
target address.

The displayed low-bit match is diagnostic. It is not a gradient, proof of a
generator, coverage, or probability gain. Generator candidates never receive
range-coverage credit.

## Scheduling and persistence

```bash
puzzleforge generator-enable --cpu-percent 10
puzzleforge local-app
```

The default worker runs for one second in each ten-second window: 10% of one
CPU core. It reserves no GPU time, so the normal BitCrack range scan continues.
The exact cursor and counters are saved atomically to `generator-lab.json` and
resume after a restart without repeating completed seed/scheme pairs.

The dashboard exposes counters, source and scheme names, and the evidence-gate
result. Seed material and a recovered private key are always redacted there.
