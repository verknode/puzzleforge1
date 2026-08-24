# Security and scope

PuzzleForge is restricted to the long-running, publicly advertised Bitcoin
Puzzle Transaction challenge.

## In scope

- registered puzzle IDs and their published key intervals;
- address derivation and candidate verification;
- range planning, benchmarking, checkpointing, and worker coordination;
- GPU acceleration for the registered challenge targets.

## Out of scope

- arbitrary wallet addresses or user-supplied target hashes;
- seed phrase, wallet file, password, or credential recovery;
- scanning unrelated blockchain addresses;
- transaction construction, signing, fee replacement, or prize sweeping;
- claims of predictive key patterns without reproducible evidence.

Do not commit found private keys, wallet material, API tokens, or worker
credentials. If a real puzzle key is found, stop workers and handle it outside
this repository using an independently reviewed operational-security plan.

