# Security and scope

PuzzleForge is restricted to the long-running, publicly advertised Bitcoin
Puzzle Transaction challenge.

## In scope

- registered puzzle IDs and their published key intervals;
- address derivation and candidate verification;
- range planning, benchmarking, checkpointing, and worker coordination;
- GPU acceleration for the registered challenge targets.
- optional local construction, signing, and broadcast of a verified reward to
  a preconfigured Bitcoin mainnet Native SegWit address.

## Out of scope

- arbitrary wallet addresses or user-supplied target hashes;
- seed phrase, wallet file, password, or credential recovery;
- scanning unrelated blockchain addresses;
- general-purpose transaction construction or signing for arbitrary wallets;
- seed phrases, imported wallet keys, or user-supplied source addresses;
- claims of predictive key patterns without reproducible evidence.

Do not commit found private keys, destination addresses, wallet material, API
tokens, or worker credentials. Auto-sweep is disabled by default and may only
use a key independently verified against the immutable puzzle registry. Signing
is local; only the raw signed transaction may leave the machine. Keep the local
profile and `sweep.json` private, verify the configured destination on the
receiving wallet, and maintain an independently reviewed recovery plan.

## Distributed deployment

- Pass the coordinator token through `PUZZLEFORGE_API_TOKEN`, never a command
  argument or repository file.
- Use HTTPS for remote workers. Plain HTTP is accepted automatically only for
  loopback addresses and requires an explicit override elsewhere.
- Treat the SQLite database as sensitive operational state and back it up.
- Do not expose the coordinator directly to the public internet without a
  maintained TLS reverse proxy, access controls, and request monitoring.

## Cloud credentials and spending

- Provider tokens are read from `VAST_API_KEY` or `RUNPOD_API_KEY`; never place
  them in command arguments, reports, coordinator leases, or worker images.
- `cloud-catalog` is read-only and `cloud-plan` is always a dry-run.
- Paid provisioning must not be implemented without immutable plan expiry,
  explicit charge limits, double confirmation, and automatic termination tests.
