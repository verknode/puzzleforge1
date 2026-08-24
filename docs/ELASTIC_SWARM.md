# Elastic Swarm

Elastic Swarm is the price/performance and lifecycle layer for rented GPU
workers. Version 0.5 implements read-only catalogs and a budget-capped dry-run
planner. It cannot purchase capacity.

## 1. Measure hardware

Do not trust model-name estimates. Qualify one machine with the solved #8 check
and a benchmark report:

```bash
puzzleforge gpu-benchmark \
  --binary ./cuBitCrack \
  --device 0 \
  --profile balanced \
  --output benchmark.json
```

## 2. Read current offers

Tokens are accepted only through environment variables.

```bash
export VAST_API_KEY="..."
puzzleforge cloud-catalog vast --gpu "RTX 4090" --output vast.json

export RUNPOD_API_KEY="..."
puzzleforge cloud-catalog runpod \
  --gpu "RTX 4090" \
  --country NO \
  --output runpod.json
```

The Vast adapter uses the documented verified/rentable/reliability filters and
can request interruptible bids. The RunPod adapter uses the v2 catalog with
availability and current pricing. Provider prices are intentionally read live
because marketplace prices change.

- Vast API lifecycle: <https://docs.vast.ai/api-reference/hello-world>
- RunPod GPU catalog: <https://docs.runpod.io/api-reference-v2/catalog/list-gpu-types>
- RunPod billing: <https://docs.runpod.io/pods/pricing>

## 3. Build a dry-run plan

```bash
puzzleforge cloud-plan vast.json \
  --puzzle 71 \
  --rate "RTX 4090=MEASURED_KEYS_PER_SECOND" \
  --max-instances 4 \
  --max-total-hourly 2.00 \
  --max-daily 20.00 \
  --max-offer-hourly 0.70 \
  --max-cost-per-quadrillion 40.00
```

Each offer is accepted or rejected with an explicit reason. Selection requires:

- a matching validated benchmark;
- acceptable benchmark spread;
- provider verification and reliability unless explicitly relaxed;
- an acceptable individual hourly price;
- an acceptable measured cost per quadrillion operations;
- remaining instance, hourly, and worst-case daily budget.

RunPod's catalog does not expose a per-host reliability score. Its offers are
therefore rejected by the default policy; reviewing Secure Cloud and adding
`--allow-unknown-reliability` is an explicit decision.

## Paid lifecycle gate

A future `cloud-apply` command must remain separate from planning and require:

1. an immutable saved plan;
2. a short expiry time and unchanged provider price;
3. an explicit maximum charge and instance count;
4. a second confirmation token;
5. automatic validation, benchmark, and coordinator admission;
6. automatic destruction on failure, budget exhaustion, or campaign stop.

Until all six controls and their failure tests exist, provider mutation methods
must not be added.
