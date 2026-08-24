# GPU benchmarking

PuzzleForge chooses settings from measured results instead of a hard-coded GPU
model table.

## Procedure

1. Probe the selected device.
2. Solve registered puzzle #8 and independently verify the result.
3. Run every tuning profile on the same deterministic control range.
4. Repeat each profile and record median, minimum, maximum, and relative spread.
5. Recommend the highest median rate, using lower spread as the tie-breaker.
6. Save the complete environment and results as an atomic JSON report.

```bash
puzzleforge gpu-benchmark \
  --binary ./cuBitCrack \
  --device 0 \
  --profile balanced \
  --repeats 3 \
  --chunk-size 0x40000000 \
  --output benchmark.json
```

Profiles contain these combinations:

| Profile | Configurations | Intended use |
|---|---:|---|
| `quick` | 4 | Initial qualification |
| `balanced` | 12 | Normal tuning |
| `full` | 27 | Careful one-time tuning |

The benchmark repeats a control range by design, so none of its work is added
to campaign coverage. It also stops immediately if the engine reports a
verified match.

## Comparing rented machines

Do not compare hourly price alone. Use the measured median rate:

```text
cost_per_10^15 = hourly_price / (median_keys_per_second * 3600 / 10^15)
```

Also reject machines with errors, high rate spread, missing devices, or a
failed #8 validation. Future cloud adapters will apply these checks before a
rented worker is admitted to a campaign.
