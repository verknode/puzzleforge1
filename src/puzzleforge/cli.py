from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import sys
import time
from dataclasses import asdict
from pathlib import Path

from .checkpoint import load_checkpoint
from .crypto import p2pkh_address_from_private_key
from .partition import ChunkPlan
from .registry import get_puzzle, puzzles
from .scanner import run_session


SECONDS_PER_YEAR = 365.25 * 24 * 60 * 60


def parse_integer(value: str) -> int:
    try:
        return int(value.replace("_", ""), 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid integer: {value}") from exc


def positive_integer(value: str) -> int:
    parsed = parse_integer(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def nonnegative_integer(value: str) -> int:
    parsed = parse_integer(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must not be negative")
    return parsed


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.2f} seconds"
    if seconds < 3600:
        return f"{seconds / 60:.2f} minutes"
    if seconds < 86400:
        return f"{seconds / 3600:.2f} hours"
    if seconds < SECONDS_PER_YEAR:
        return f"{seconds / 86400:.2f} days"
    return f"{seconds / SECONDS_PER_YEAR:,.2f} years"


def command_list(_: argparse.Namespace) -> int:
    print("ID  STATUS     RANGE                              ADDRESS")
    for puzzle in puzzles():
        interval = f"{puzzle.start_hex}:{puzzle.end_hex}"
        print(f"{puzzle.number:<3} {puzzle.status:<10} {interval:<34} {puzzle.address}")
    return 0


def command_inspect(args: argparse.Namespace) -> int:
    puzzle = get_puzzle(args.puzzle)
    print(f"Puzzle:       #{puzzle.number}")
    print(f"Status:       {puzzle.status}")
    print(f"Address:      {puzzle.address}")
    print(f"Start:        {puzzle.start_hex}")
    print(f"End:          {puzzle.end_hex}")
    print(f"Candidates:   {puzzle.size:,} (2^{puzzle.number - 1})")
    return 0


def _make_plan(args: argparse.Namespace) -> ChunkPlan:
    return ChunkPlan(
        puzzle=get_puzzle(args.puzzle),
        chunk_size=args.chunk_size,
        seed=args.seed,
        shard_count=args.shards,
        shard_index=args.shard_index,
    )


def command_plan(args: argparse.Namespace) -> int:
    plan = _make_plan(args)
    manifest = plan.manifest()
    preview = []
    for sequence in range(min(args.preview, plan.chunks_for_shard)):
        chunk = plan.chunk_for_sequence(sequence)
        preview.append(
            {
                "sequence": sequence,
                "ordinal": chunk.ordinal,
                "chunk_id": chunk.chunk_id,
                "start": f"{chunk.start:x}",
                "end": f"{chunk.end:x}",
                "keys": chunk.size,
            }
        )
    print(json.dumps({"manifest": manifest, "preview": preview}, indent=2))
    return 0


def command_estimate(args: argparse.Namespace) -> int:
    puzzle = get_puzzle(args.puzzle)
    if args.rate <= 0:
        raise ValueError("rate must be positive")
    if args.days < 0:
        raise ValueError("days must not be negative")
    total_rate = args.rate * args.devices
    full_seconds = puzzle.size / total_rate
    expected_seconds = full_seconds / 2
    checked = min(puzzle.size, total_rate * args.days * 86400)
    probability = checked / puzzle.size
    print(f"Puzzle:                  #{puzzle.number}")
    print(f"Aggregate rate:           {total_rate:,.0f} keys/s")
    print(f"Full-range time:          {_format_duration(full_seconds)}")
    print(f"Expected time (mean):     {_format_duration(expected_seconds)}")
    print(f"Coverage after {args.days:g} days: {probability:.12%}")
    return 0


def _default_checkpoint(args: argparse.Namespace) -> Path:
    return Path(
        f".puzzleforge/puzzle-{args.puzzle}-shard-{args.shard_index}-of-{args.shards}.json"
    )


def command_scan(args: argparse.Namespace) -> int:
    plan = _make_plan(args)
    checkpoint = args.checkpoint or _default_checkpoint(args)
    result = run_session(
        plan=plan,
        chunks=args.chunks,
        workers=args.workers,
        checkpoint_path=checkpoint,
    )
    print(f"Checked this run: {result.checked:,}")
    print(f"Chunks completed: {result.completed_chunks:,}")
    print(f"Checkpoint:       {checkpoint}")
    if result.found_key is not None:
        print(f"MATCH FOUND:      {result.found_key:064x}")
        return 0
    if result.exhausted:
        print("Shard exhausted without a match.")
    else:
        print("No match in completed chunks; run the same command to resume.")
    return 1


def command_status(args: argparse.Namespace) -> int:
    checkpoint = load_checkpoint(args.checkpoint)
    print(json.dumps(asdict(checkpoint), indent=2, sort_keys=True))
    return 0


def command_verify(args: argparse.Namespace) -> int:
    puzzle = get_puzzle(args.puzzle)
    if not puzzle.start <= args.private_key <= puzzle.end:
        print("Key is outside the published interval.", file=sys.stderr)
        return 2
    address = p2pkh_address_from_private_key(args.private_key)
    matches = address == puzzle.address
    print(f"Derived address: {address}")
    print(f"Target address:  {puzzle.address}")
    print(f"Match:           {'yes' if matches else 'no'}")
    return 0 if matches else 1


def command_mosaic_preview(args: argparse.Namespace) -> int:
    from .mosaic import MosaicPlanner

    puzzle = get_puzzle(args.puzzle)
    total_chunks = (puzzle.size + args.chunk_size - 1) // args.chunk_size
    planner = MosaicPlanner(total_chunks, seed=args.seed)
    candidates = []
    checked_keys = 0
    for candidate in planner.preview(args.preview):
        start = puzzle.start + candidate.chunk_id * args.chunk_size
        end = min(start + args.chunk_size - 1, puzzle.end)
        checked_keys += end - start + 1
        candidates.append(
            {
                **candidate.to_dict(),
                "start": f"{start:x}",
                "end": f"{end:x}",
                "keys": end - start + 1,
            }
        )
    print(
        json.dumps(
            {
                "experimental": True,
                "uniform_target_advantage_claimed": False,
                "puzzle": puzzle.number,
                "seed": args.seed,
                "chunk_size": args.chunk_size,
                "total_chunks": total_chunks,
                "preview_unique_keys": checked_keys,
                "planner_state": planner.state(),
                "preview": candidates,
            },
            indent=2,
        )
    )
    return 0


def _engine_from_args(args: argparse.Namespace):
    from .engine import BitCrackEngine, EngineTuning

    return BitCrackEngine(
        binary=args.binary,
        tuning=EngineTuning(
            device=args.device,
            blocks=args.blocks,
            threads=args.threads,
            points=args.points,
        ),
        timeout_seconds=args.engine_timeout,
    )


def command_token(_: argparse.Namespace) -> int:
    print(secrets.token_urlsafe(32))
    return 0


def command_gpu_probe(args: argparse.Namespace) -> int:
    print(_engine_from_args(args).probe())
    return 0


def command_gpu_test(args: argparse.Namespace) -> int:
    from .partition import KeyChunk

    puzzle = get_puzzle(8)
    chunk = KeyChunk(
        ordinal=0,
        chunk_id=0,
        start=puzzle.start,
        end=puzzle.end,
    )
    outcome = _engine_from_args(args).scan(puzzle, chunk)
    if outcome.status != "found" or outcome.found_key != 0xE0:
        print(f"FAIL: {outcome.message}", file=sys.stderr)
        return 1
    print("PASS: GPU engine solved puzzle #8 and PuzzleForge verified the result.")
    print(f"Rate: {outcome.rate_keys_per_second:,.0f} keys/s")
    return 0


def command_gpu_benchmark(args: argparse.Namespace) -> int:
    from .benchmark import (
        run_benchmark,
        save_report,
        tuning_profiles,
        validate_known_puzzle,
    )
    from .engine import BitCrackEngine, EngineTuning

    validation_engine = BitCrackEngine(
        args.binary,
        EngineTuning(device=args.device),
        timeout_seconds=args.engine_timeout,
    )
    device_probe = validation_engine.probe()
    validate_known_puzzle(validation_engine)
    report = run_benchmark(
        puzzle_number=args.puzzle,
        chunk_size=args.chunk_size,
        seed=args.seed,
        planner_mode=args.mode,
        sequence=args.sequence,
        repeats=args.repeats,
        profiles=tuning_profiles(args.profile, args.device),
        engine_factory=lambda tuning: BitCrackEngine(
            args.binary,
            tuning,
            timeout_seconds=args.engine_timeout,
        ),
        binary_name=args.binary.name,
        device_probe=device_probe,
    )
    if args.output:
        save_report(args.output, report)
        print(f"Report: {args.output}")
    else:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    if report.best is None:
        print("No tuning profile completed successfully.", file=sys.stderr)
        return 1
    print(f"Best rate: {report.best.median_keys_per_second:,.0f} keys/s")
    print(f"Use flags: {report.to_dict()['recommended_flags']}")
    return 0


def command_coordinator_init(args: argparse.Namespace) -> int:
    from .coordinator import Coordinator

    coordinator = Coordinator.initialize(
        args.database,
        puzzle_number=args.puzzle,
        chunk_size=args.chunk_size,
        seed=args.seed,
    )
    print(json.dumps(coordinator.status(), indent=2, sort_keys=True))
    return 0


def command_coordinator_status(args: argparse.Namespace) -> int:
    from .coordinator import Coordinator

    print(json.dumps(Coordinator(args.database).status(), indent=2, sort_keys=True))
    return 0


def _token_from_environment(name: str) -> str:
    token = os.environ.get(name)
    if not token:
        raise ValueError(f"environment variable {name} is not set")
    return token


def command_coordinator_serve(args: argparse.Namespace) -> int:
    from .coordinator import Coordinator
    from .coordinator_http import serve

    serve(
        Coordinator(args.database),
        api_token=_token_from_environment(args.token_env),
        host=args.host,
        port=args.port,
    )
    return 0


def command_gpu_worker(args: argparse.Namespace) -> int:
    from .remote import CoordinatorClient, GPUWorker

    client = CoordinatorClient(
        args.coordinator,
        _token_from_environment(args.token_env),
        allow_insecure_http=args.allow_insecure_http,
    )
    worker = GPUWorker(
        client,
        _engine_from_args(args),
        worker=args.worker,
        lease_seconds=args.lease_seconds,
    )
    completed = 0
    try:
        while args.max_chunks is None or completed < args.max_chunks:
            result = worker.run_once()
            print(result.message, flush=True)
            if result.found:
                return 0
            if result.outcome == "complete":
                completed += 1
            if result.outcome == "idle":
                state = client.status().get("state")
                if state in {"found", "exhausted"} or args.once:
                    return 0
                time.sleep(args.idle_seconds)
            elif args.once:
                return 0 if result.outcome == "complete" else 1
    except KeyboardInterrupt:
        print("Worker stopped.", file=sys.stderr)
        return 130
    return 0


def command_cloud_catalog(args: argparse.Namespace) -> int:
    from .cloud import save_json_atomic
    from .cloud_providers import RunPodClient, VastClient

    token_env = args.token_env or (
        "VAST_API_KEY" if args.provider == "vast" else "RUNPOD_API_KEY"
    )
    token = _token_from_environment(token_env)
    if args.provider == "vast":
        offers = VastClient(token).search_offers(
            gpu_models=tuple(args.gpu),
            interruptible=not args.on_demand,
            minimum_reliability=args.min_reliability,
            limit=args.limit,
        )
    else:
        offers = RunPodClient(token).catalog_offers(
            cloud="COMMUNITY" if args.community else "SECURE",
            minimum_cuda=args.minimum_cuda,
            countries=tuple(args.country),
        )
        if args.gpu:
            wanted = {value.upper() for value in args.gpu}
            offers = [
                offer
                for offer in offers
                if offer.gpu_model.upper() in wanted
                or offer.offer_id.split(":", 1)[0].upper() in wanted
            ]
    payload = {
        "schema": 1,
        "read_only": True,
        "provider": args.provider,
        "offers": [offer.to_dict() for offer in offers],
    }
    if args.output:
        save_json_atomic(args.output, payload)
        print(f"Catalog: {args.output}")
        print(f"Offers:  {len(offers)}")
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _benchmark_rate(value: str) -> tuple[str, float]:
    model, separator, raw_rate = value.rpartition("=")
    if not separator or not model.strip():
        raise argparse.ArgumentTypeError("rate must use GPU_MODEL=KEYS_PER_SECOND")
    try:
        rate = float(raw_rate.replace("_", ""))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("benchmark rate must be a number") from exc
    if rate <= 0:
        raise argparse.ArgumentTypeError("benchmark rate must be positive")
    return model.strip(), rate


def command_cloud_plan(args: argparse.Namespace) -> int:
    from .cloud import BenchmarkRate, CloudOffer, CloudPolicy, plan_capacity

    raw = json.loads(args.catalog.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("offers"), list):
        raise ValueError("cloud catalog must contain an offers array")
    offers = [CloudOffer.from_dict(item) for item in raw["offers"]]
    benchmarks = [BenchmarkRate(model, rate) for model, rate in args.rate]
    policy = CloudPolicy(
        max_instances=args.max_instances,
        max_total_hourly_usd=args.max_total_hourly,
        max_daily_usd=args.max_daily,
        max_offer_hourly_usd=args.max_offer_hourly,
        max_cost_per_quadrillion_usd=args.max_cost_per_quadrillion,
        min_reliability=args.min_reliability,
        max_benchmark_spread=args.max_benchmark_spread,
        allow_interruptible=not args.no_interruptible,
        require_verified=not args.allow_unverified,
        allow_unknown_reliability=args.allow_unknown_reliability,
    )
    plan = plan_capacity(
        puzzle_number=args.puzzle,
        offers=offers,
        benchmarks=benchmarks,
        policy=policy,
        running_instances=args.running_instances,
        running_hourly_usd=args.running_hourly,
        spent_today_usd=args.spent_today,
        hours_remaining_today=args.hours_remaining,
    )
    print(json.dumps(plan.to_dict(), indent=2, sort_keys=True))
    return 0


def add_plan_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("puzzle", type=int, choices=[p.number for p in puzzles()])
    parser.add_argument("--chunk-size", type=positive_integer, default=100_000)
    parser.add_argument("--seed", default="puzzleforge-default")
    parser.add_argument("--shards", type=positive_integer, default=1)
    parser.add_argument("--shard-index", type=nonnegative_integer, default=0)


def add_engine_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--device", type=nonnegative_integer)
    parser.add_argument("--blocks", type=positive_integer)
    parser.add_argument("--threads", type=positive_integer)
    parser.add_argument("--points", type=positive_integer)
    parser.add_argument("--engine-timeout", type=float)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="puzzleforge",
        description="Challenge-scoped toolkit for the public Bitcoin Puzzle Transaction",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="list reviewed puzzle targets")
    list_parser.set_defaults(handler=command_list)

    inspect_parser = subparsers.add_parser("inspect", help="show one puzzle")
    inspect_parser.add_argument("puzzle", type=int, choices=[p.number for p in puzzles()])
    inspect_parser.set_defaults(handler=command_inspect)

    plan_parser = subparsers.add_parser("plan", help="preview deterministic chunks")
    add_plan_arguments(plan_parser)
    plan_parser.add_argument("--preview", type=positive_integer, default=5)
    plan_parser.set_defaults(handler=command_plan)

    estimate_parser = subparsers.add_parser("estimate", help="estimate time and probability")
    estimate_parser.add_argument("puzzle", type=int, choices=[p.number for p in puzzles()])
    estimate_parser.add_argument("--rate", type=float, required=True, help="keys/s per device")
    estimate_parser.add_argument("--devices", type=positive_integer, default=1)
    estimate_parser.add_argument("--days", type=float, default=365.25)
    estimate_parser.set_defaults(handler=command_estimate)

    scan_parser = subparsers.add_parser("scan", help="run the correctness-oriented CPU scanner")
    add_plan_arguments(scan_parser)
    scan_parser.add_argument("--chunks", type=positive_integer, default=1)
    scan_parser.add_argument("--workers", type=positive_integer, default=os.cpu_count() or 1)
    scan_parser.add_argument("--checkpoint", type=Path)
    scan_parser.set_defaults(handler=command_scan)

    status_parser = subparsers.add_parser("status", help="show a checkpoint")
    status_parser.add_argument("checkpoint", type=Path)
    status_parser.set_defaults(handler=command_status)

    verify_parser = subparsers.add_parser("verify", help="verify a candidate for a reviewed puzzle")
    verify_parser.add_argument("puzzle", type=int, choices=[p.number for p in puzzles()])
    verify_parser.add_argument("private_key", type=parse_integer)
    verify_parser.set_defaults(handler=command_verify)

    mosaic_parser = subparsers.add_parser(
        "mosaic-preview", help="preview the experimental multi-order scheduler"
    )
    mosaic_parser.add_argument(
        "puzzle", type=int, choices=[p.number for p in puzzles()]
    )
    mosaic_parser.add_argument(
        "--chunk-size", type=positive_integer, default=1 << 32
    )
    mosaic_parser.add_argument("--seed", default="puzzleforge-mosaic-v1")
    mosaic_parser.add_argument("--preview", type=positive_integer, default=16)
    mosaic_parser.set_defaults(handler=command_mosaic_preview)

    token_parser = subparsers.add_parser("token", help="generate a coordinator API token")
    token_parser.set_defaults(handler=command_token)

    gpu_probe_parser = subparsers.add_parser(
        "gpu-probe", help="list devices visible to BitCrack"
    )
    add_engine_arguments(gpu_probe_parser)
    gpu_probe_parser.set_defaults(handler=command_gpu_probe)

    gpu_test_parser = subparsers.add_parser(
        "gpu-test", help="run the solved puzzle #8 end-to-end test"
    )
    add_engine_arguments(gpu_test_parser)
    gpu_test_parser.set_defaults(handler=command_gpu_test)

    gpu_benchmark_parser = subparsers.add_parser(
        "gpu-benchmark", help="validate and auto-tune BitCrack settings"
    )
    gpu_benchmark_parser.add_argument("--binary", type=Path, required=True)
    gpu_benchmark_parser.add_argument("--device", type=nonnegative_integer)
    gpu_benchmark_parser.add_argument("--engine-timeout", type=float)
    gpu_benchmark_parser.add_argument(
        "--puzzle",
        type=int,
        choices=[p.number for p in puzzles() if p.status == "unsolved"],
        default=71,
    )
    gpu_benchmark_parser.add_argument(
        "--chunk-size", type=positive_integer, default=1 << 30
    )
    gpu_benchmark_parser.add_argument("--seed", default="puzzleforge-benchmark-v1")
    gpu_benchmark_parser.add_argument(
        "--sequence", type=nonnegative_integer, default=0
    )
    gpu_benchmark_parser.add_argument(
        "--profile", choices=("quick", "balanced", "full"), default="quick"
    )
    gpu_benchmark_parser.add_argument("--repeats", type=positive_integer, default=2)
    gpu_benchmark_parser.add_argument("--output", type=Path)
    gpu_benchmark_parser.set_defaults(handler=command_gpu_benchmark)

    coordinator_init_parser = subparsers.add_parser(
        "coordinator-init", help="create a distributed campaign database"
    )
    coordinator_init_parser.add_argument("database", type=Path)
    coordinator_init_parser.add_argument(
        "puzzle", type=int, choices=[p.number for p in puzzles()]
    )
    coordinator_init_parser.add_argument(
        "--chunk-size", type=positive_integer, default=1 << 32
    )
    coordinator_init_parser.add_argument("--seed", default="puzzleforge-distributed")
    coordinator_init_parser.add_argument(
        "--mode", choices=("affine", "mosaic"), default="affine"
    )
    coordinator_init_parser.set_defaults(handler=command_coordinator_init)

    coordinator_status_parser = subparsers.add_parser(
        "coordinator-status", help="show exact distributed campaign coverage"
    )
    coordinator_status_parser.add_argument("database", type=Path)
    coordinator_status_parser.set_defaults(handler=command_coordinator_status)

    coordinator_serve_parser = subparsers.add_parser(
        "coordinator-serve", help="serve the distributed worker API"
    )
    coordinator_serve_parser.add_argument("database", type=Path)
    coordinator_serve_parser.add_argument("--host", default="127.0.0.1")
    coordinator_serve_parser.add_argument("--port", type=positive_integer, default=8787)
    coordinator_serve_parser.add_argument(
        "--token-env", default="PUZZLEFORGE_API_TOKEN"
    )
    coordinator_serve_parser.set_defaults(handler=command_coordinator_serve)

    gpu_worker_parser = subparsers.add_parser(
        "gpu-worker", help="run a BitCrack worker against a coordinator"
    )
    gpu_worker_parser.add_argument("--coordinator", required=True)
    gpu_worker_parser.add_argument("--worker", default=socket.gethostname())
    gpu_worker_parser.add_argument(
        "--token-env", default="PUZZLEFORGE_API_TOKEN"
    )
    gpu_worker_parser.add_argument("--lease-seconds", type=positive_integer, default=900)
    gpu_worker_parser.add_argument("--idle-seconds", type=positive_integer, default=15)
    gpu_worker_parser.add_argument("--max-chunks", type=positive_integer)
    gpu_worker_parser.add_argument("--once", action="store_true")
    gpu_worker_parser.add_argument("--allow-insecure-http", action="store_true")
    add_engine_arguments(gpu_worker_parser)
    gpu_worker_parser.set_defaults(handler=command_gpu_worker)

    cloud_catalog_parser = subparsers.add_parser(
        "cloud-catalog", help="read current GPU offers without renting anything"
    )
    cloud_catalog_parser.add_argument("provider", choices=("vast", "runpod"))
    cloud_catalog_parser.add_argument("--token-env")
    cloud_catalog_parser.add_argument("--gpu", action="append", default=[])
    cloud_catalog_parser.add_argument("--country", action="append", default=[])
    cloud_catalog_parser.add_argument("--minimum-cuda", default="11.8")
    cloud_catalog_parser.add_argument("--min-reliability", type=float, default=0.98)
    cloud_catalog_parser.add_argument("--limit", type=positive_integer, default=100)
    cloud_catalog_parser.add_argument("--on-demand", action="store_true")
    cloud_catalog_parser.add_argument("--community", action="store_true")
    cloud_catalog_parser.add_argument("--output", type=Path)
    cloud_catalog_parser.set_defaults(handler=command_cloud_catalog)

    cloud_plan_parser = subparsers.add_parser(
        "cloud-plan", help="build a budget-capped dry-run capacity plan"
    )
    cloud_plan_parser.add_argument("catalog", type=Path)
    cloud_plan_parser.add_argument(
        "--puzzle", type=int, choices=[p.number for p in puzzles()], default=71
    )
    cloud_plan_parser.add_argument(
        "--rate", type=_benchmark_rate, action="append", required=True
    )
    cloud_plan_parser.add_argument("--max-instances", type=positive_integer, default=1)
    cloud_plan_parser.add_argument("--max-total-hourly", type=float, required=True)
    cloud_plan_parser.add_argument("--max-daily", type=float, required=True)
    cloud_plan_parser.add_argument("--max-offer-hourly", type=float, required=True)
    cloud_plan_parser.add_argument(
        "--max-cost-per-quadrillion", type=float, required=True
    )
    cloud_plan_parser.add_argument("--min-reliability", type=float, default=0.98)
    cloud_plan_parser.add_argument(
        "--max-benchmark-spread", type=float, default=0.20
    )
    cloud_plan_parser.add_argument("--allow-unknown-reliability", action="store_true")
    cloud_plan_parser.add_argument("--allow-unverified", action="store_true")
    cloud_plan_parser.add_argument("--no-interruptible", action="store_true")
    cloud_plan_parser.add_argument(
        "--running-instances", type=nonnegative_integer, default=0
    )
    cloud_plan_parser.add_argument("--running-hourly", type=float, default=0.0)
    cloud_plan_parser.add_argument("--spent-today", type=float, default=0.0)
    cloud_plan_parser.add_argument("--hours-remaining", type=float, default=24.0)
    cloud_plan_parser.set_defaults(handler=command_cloud_plan)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        raise SystemExit(args.handler(args))
    except (ValueError, OSError, RuntimeError, json.JSONDecodeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
