from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

from .checkpoint import load_checkpoint
from .crypto import DEFAULT_BATCH_SIZE, p2pkh_address_from_private_key
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
        batch_size=args.batch_size,
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


def command_cold_preview(args: argparse.Namespace) -> int:
    from .coldzone import COMPONENTS, ColdOrder
    from .mosaic import COLD_LANES, MosaicPlanner

    puzzle = get_puzzle(args.puzzle)
    total_chunks = (puzzle.size + args.chunk_size - 1) // args.chunk_size
    order = ColdOrder(total_chunks, args.seed, puzzle.number, bands=args.bands)
    planner = MosaicPlanner(
        total_chunks,
        seed=args.seed,
        lanes=COLD_LANES,
        target_puzzle=puzzle.number,
    )

    bands = []
    for row in order.band_report(args.bands_preview):
        low = puzzle.start + int(row["start_fraction"] * puzzle.size)
        high = puzzle.start + int(row["end_fraction"] * puzzle.size) - 1
        bands.append({**row, "start": f"{low:x}", "end": f"{high:x}"})

    candidates = []
    for candidate in planner.preview(args.preview):
        start = puzzle.start + candidate.chunk_id * args.chunk_size
        end = min(start + args.chunk_size - 1, puzzle.end)
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
                "note": (
                    "Ordering only. The chunk set and the coverage probability "
                    "for a given number of unique keys are unchanged. The cold "
                    "prior models public search behaviour; it is not measured "
                    "telemetry and is not a cryptographic shortcut."
                ),
                "puzzle": puzzle.number,
                "seed": args.seed,
                "chunk_size": args.chunk_size,
                "total_chunks": total_chunks,
                "bands": order.bands,
                "components": [
                    {
                        "name": component.name,
                        "weight": component.weight,
                        "rationale": component.rationale,
                    }
                    for component in COMPONENTS
                ],
                "coldest_bands": bands,
                "lanes": [
                    {"name": lane.name, "weight": lane.weight} for lane in COLD_LANES
                ],
                "candidates": candidates,
            },
            indent=2,
        )
    )
    return 0


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


def command_hypothesis_preview(args: argparse.Namespace) -> int:
    from .hypothesis import HypothesisPlanner

    puzzle = get_puzzle(args.puzzle)
    total_chunks = (puzzle.size + args.chunk_size - 1) // args.chunk_size
    planner = HypothesisPlanner(
        total_chunks,
        target_puzzle=puzzle.number,
        seed=args.seed,
    )
    seen: set[int] = set()
    preview = []
    for _ in range(min(args.preview, total_chunks)):
        candidate = planner.next_unseen(seen)
        seen.add(candidate.chunk_id)
        start = puzzle.start + candidate.chunk_id * args.chunk_size
        end = min(start + args.chunk_size - 1, puzzle.end)
        preview.append(
            {
                "cycle": candidate.cycle,
                "analysis_performed": candidate.analysis_performed,
                "model": candidate.model,
                "model_validated": candidate.model_validated,
                "cell": candidate.cell,
                "chunk_id": candidate.chunk_id,
                "start": f"{start:x}",
                "end": f"{end:x}",
                "keys": end - start + 1,
            }
        )
    report = planner.last_report
    if report is not None:
        report = dict(report)
        scores = report.get("scores")
        if isinstance(scores, list):
            report["scores"] = scores[: args.top_models]
    print(
        json.dumps(
            {
                "experimental": True,
                "guaranteed_probability_lift": False,
                "ratio": {"research_percent": 10, "search_percent": 90},
                "report": report,
                "preview": preview,
            },
            indent=2,
            sort_keys=True,
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


def command_local_setup(args: argparse.Namespace) -> int:
    from .benchmark import (
        run_benchmark,
        save_report,
        tuning_profiles,
        validate_known_puzzle,
    )
    from .coordinator import Coordinator
    from .engine import BitCrackEngine, EngineTuning
    from .local import (
        LocalProfile,
        find_bitcrack_binary,
        recommended_chunk_size,
        save_profile,
        utc_now,
    )
    from .thermal import ThermalPolicy

    ThermalPolicy(
        maximum_c=args.max_temp,
        resume_c=args.resume_temp,
        poll_seconds=args.thermal_poll_seconds,
        max_retries=args.thermal_retries,
    )

    seed = args.seed or f"local-{secrets.token_hex(32)}"
    state_dir = args.state_dir.expanduser().resolve()
    profile_path = state_dir / "profile.json"
    database_path = state_dir / "campaign.sqlite3"
    benchmark_path = state_dir / "benchmark.json"
    existing = [
        path for path in (profile_path, database_path, benchmark_path) if path.exists()
    ]
    if existing:
        raise FileExistsError(
            f"local setup already exists at {state_dir}; use local-run to resume"
        )

    binary = find_bitcrack_binary(args.binary)
    validation_engine = BitCrackEngine(
        binary,
        EngineTuning(device=args.device),
        timeout_seconds=args.engine_timeout,
    )
    device_probe = validation_engine.probe()
    validate_known_puzzle(validation_engine)
    report = run_benchmark(
        puzzle_number=args.puzzle,
        chunk_size=args.benchmark_chunk_size,
        seed=seed,
        sequence=0,
        repeats=args.repeats,
        profiles=tuning_profiles(args.benchmark_profile, args.device),
        engine_factory=lambda tuning: BitCrackEngine(
            binary,
            tuning,
            timeout_seconds=args.engine_timeout,
        ),
        binary_name=binary.name,
        device_probe=device_probe,
    )
    if report.best is None:
        raise RuntimeError("no local tuning profile completed successfully")

    state_dir.mkdir(parents=True, exist_ok=True)
    save_report(benchmark_path, report)
    puzzle = get_puzzle(args.puzzle)
    chunk_size = recommended_chunk_size(
        report.best.median_keys_per_second,
        args.chunk_seconds,
        maximum_keys=min(puzzle.size, (1 << 63) - 1),
    )
    Coordinator.initialize(
        database_path,
        puzzle_number=args.puzzle,
        chunk_size=chunk_size,
        seed=seed,
        planner_mode=args.mode,
    )
    profile = LocalProfile(
        schema=1,
        puzzle=args.puzzle,
        binary=str(binary),
        tuning=report.best.tuning,
        measured_rate_keys_per_second=report.best.median_keys_per_second,
        benchmark_relative_spread=report.best.relative_spread,
        chunk_size=chunk_size,
        target_chunk_seconds=args.chunk_seconds,
        planner_mode=args.mode,
        seed=seed,
        database=str(database_path),
        benchmark_report=str(benchmark_path),
        device_probe=device_probe,
        created_at=utc_now(),
        max_temperature_c=args.max_temp,
        resume_temperature_c=args.resume_temp,
        thermal_poll_seconds=args.thermal_poll_seconds,
        thermal_max_retries=args.thermal_retries,
        hypothesis_enabled=args.mode == "hypothesis",
        hypothesis_research_percent=10,
        hypothesis_search_percent=90,
        generator_lab_enabled=True,
        generator_lab_cpu_percent=10,
    )
    save_profile(profile_path, profile)

    print(f"Local profile: {profile_path}")
    print(f"GPU rate:      {profile.measured_rate_keys_per_second:,.0f} keys/s")
    print(f"Chunk target:  {profile.chunk_size:,} keys")
    print("Validation:    PASS (solved puzzle #8)")
    if profile.hypothesis_enabled:
        print("Hypothesis:    10% research / 90% GPU search")
    print("Generator Lab: 10% of one CPU core / GPU unchanged")
    return 0


def command_local_run(args: argparse.Namespace) -> int:
    return _run_local_campaign(args)


def command_local_reseed(args: argparse.Namespace) -> int:
    from .coordinator import Coordinator
    from .local import load_profile, save_profile

    profile = load_profile(args.profile)
    seed = args.seed or f"local-{secrets.token_hex(32)}"
    fingerprint = Coordinator(Path(profile.database)).reseed(seed)
    save_profile(args.profile, replace(profile, seed=seed))
    print("Private work order changed; existing coverage retained.")
    print(f"Seed fingerprint: {fingerprint}")
    return 0


def command_hypothesis_enable(args: argparse.Namespace) -> int:
    from .coordinator import Coordinator
    from .local import load_profile, save_profile

    profile = load_profile(args.profile)
    if profile.puzzle < 18:
        raise ValueError("Hypothesis Lab requires a target after puzzle #17")
    updated = replace(
        profile,
        planner_mode="hypothesis",
        hypothesis_enabled=True,
        hypothesis_research_percent=10,
        hypothesis_search_percent=90,
    )
    save_profile(args.profile, updated)
    Coordinator(Path(updated.database)).enable_hypothesis(
        research_percent=10,
        search_percent=90,
    )
    print("Hypothesis Lab enabled: 10% research / 90% GPU search")
    return 0


def command_generator_enable(args: argparse.Namespace) -> int:
    from .generator_lab import default_generator_lab
    from .local import load_profile, save_profile

    profile = load_profile(args.profile)
    wordlist = (
        profile.generator_lab_wordlist
        if args.wordlist is None
        else str(args.wordlist.expanduser().resolve())
    )
    updated = replace(
        profile,
        generator_lab_enabled=True,
        generator_lab_cpu_percent=args.cpu_percent,
        generator_lab_wordlist=wordlist,
    )
    lab = default_generator_lab(
        updated.database,
        target_puzzle=updated.puzzle,
        wordlist=updated.generator_lab_wordlist,
    )
    lab.ensure_state()
    save_profile(args.profile, updated)
    print(
        "Generator Lab enabled: "
        f"{updated.generator_lab_cpu_percent}% of one CPU core; GPU search unchanged"
    )
    if updated.generator_lab_wordlist:
        print(f"Wordlist: {updated.generator_lab_wordlist}")
    return 0


def command_local_sweep_configure(args: argparse.Namespace) -> int:
    from .local import load_profile, save_profile
    from .sweep import decode_mainnet_p2wpkh

    profile = load_profile(args.profile)
    if args.disable:
        updated = replace(profile, auto_sweep_enabled=False)
        save_profile(args.profile, updated)
        print("Auto-sweep disabled. The saved destination was retained.")
        return 0
    if not args.address:
        raise ValueError("a bc1q destination address is required")
    decode_mainnet_p2wpkh(args.address)
    updated = replace(
        profile,
        auto_sweep_enabled=True,
        sweep_address=args.address.lower(),
        sweep_fee_floor_sat_vb=args.fee_floor,
        sweep_fee_cap_sat_vb=args.fee_cap,
    )
    save_profile(args.profile, updated)
    print("AUTO-SWEEP ENABLED")
    print("Network:      Bitcoin mainnet")
    print(f"Destination:  {updated.sweep_address}")
    print(
        "Fee policy:   fastest available estimate, "
        f"bounded to {updated.sweep_fee_floor_sat_vb}-"
        f"{updated.sweep_fee_cap_sat_vb} sat/vB"
    )
    print("Private key:  local signing only; never sent to an API")
    return 0


def _run_local_campaign(args: argparse.Namespace) -> int:
    from .coordinator import Coordinator
    from .local import (
        engine_from_profile,
        load_profile,
        resume_pending_sweep,
        run_local_once,
    )

    profile = load_profile(args.profile)
    coordinator = Coordinator(Path(profile.database))
    if profile.hypothesis_enabled:
        coordinator.enable_hypothesis(
            research_percent=profile.hypothesis_research_percent,
            search_percent=profile.hypothesis_search_percent,
        )
    campaign_status = coordinator.status()
    if campaign_status["state"] == "found":
        receipt = resume_pending_sweep(profile)
        if receipt is not None and receipt.broadcast:
            print(f"AUTO-SWEEP BROADCAST txid={receipt.txid}")
            return 0
        if receipt is not None:
            print(f"AUTO-SWEEP PENDING: {receipt.detail}", file=sys.stderr)
            return 1
        print("Campaign already found a verified match; no work remains.")
        return 0
    engine = engine_from_profile(
        profile,
        timeout_seconds=args.engine_timeout,
        thermal_guard=not args.no_thermal_guard,
    )
    generator_worker = None
    if profile.generator_lab_enabled:
        from .generator_lab import GeneratorLabWorker, default_generator_lab

        generator_lab = default_generator_lab(
            profile.database,
            target_puzzle=profile.puzzle,
            wordlist=profile.generator_lab_wordlist,
        )
        existing_key = generator_lab.found_key()
        if existing_key is not None:
            return _finish_generator_match(profile, existing_key, coordinator)
        generator_worker = GeneratorLabWorker(
            generator_lab,
            duty_percent=profile.generator_lab_cpu_percent,
        )
        generator_worker.start()
    completed = 0
    try:
        while args.max_chunks is None or completed < args.max_chunks:
            result = run_local_once(
                profile,
                engine,
                worker=args.worker,
                lease_seconds=args.lease_seconds,
            )
            print(result.message, flush=True)
            if result.found:
                return 0
            if generator_worker is not None:
                generator_key = generator_worker.found_key()
                if generator_key is not None:
                    return _finish_generator_match(
                        profile,
                        generator_key,
                        coordinator,
                    )
            if result.outcome == "complete":
                completed += 1
                continue
            if result.outcome == "idle":
                return 0
            return 1
    except KeyboardInterrupt:
        print("Local run stopped; unfinished work was returned to the queue.")
        return 130
    finally:
        if generator_worker is not None:
            generator_worker.stop()
    return 0


def _finish_generator_match(profile, private_key: int, coordinator) -> int:
    from .local import resume_pending_sweep

    coordinator.record_verified_candidate(f"{private_key:064x}")
    print("GENERATOR MATCH VERIFIED against the registered puzzle address.")
    receipt = resume_pending_sweep(profile)
    if receipt is not None and receipt.broadcast:
        print(f"AUTO-SWEEP BROADCAST txid={receipt.txid}")
        return 0
    if receipt is not None:
        print(f"AUTO-SWEEP PENDING: {receipt.detail}", file=sys.stderr)
        return 1
    return 0


def command_local_status(args: argparse.Namespace) -> int:
    from .coordinator import Coordinator
    from .generator_lab import generator_dashboard_status
    from .local import load_profile

    profile = load_profile(args.profile)
    payload = {
        "local": {
            "binary": profile.binary,
            "tuning": asdict(profile.tuning),
            "measured_rate_keys_per_second": profile.measured_rate_keys_per_second,
            "chunk_size": profile.chunk_size,
            "target_chunk_seconds": profile.target_chunk_seconds,
            "benchmark_relative_spread": profile.benchmark_relative_spread,
            "thermal_guard": {
                "maximum_c": profile.max_temperature_c,
                "resume_c": profile.resume_temperature_c,
                "poll_seconds": profile.thermal_poll_seconds,
                "max_retries": profile.thermal_max_retries,
            },
            "hypothesis_lab": {
                "enabled": profile.hypothesis_enabled,
                "research_percent": profile.hypothesis_research_percent,
                "search_percent": profile.hypothesis_search_percent,
            },
            "generator_lab": {
                "enabled": profile.generator_lab_enabled,
                "cpu_duty_percent": profile.generator_lab_cpu_percent,
                "gpu_reserved_percent": 0,
                "wordlist_configured": bool(profile.generator_lab_wordlist),
            },
            "auto_sweep": {
                "enabled": profile.auto_sweep_enabled,
                "destination_address": profile.sweep_address,
                "fee_floor_sat_vb": profile.sweep_fee_floor_sat_vb,
                "fee_cap_sat_vb": profile.sweep_fee_cap_sat_vb,
            },
        },
        "campaign": Coordinator(Path(profile.database)).status(),
        "generator_lab": generator_dashboard_status(
            profile.database,
            enabled=profile.generator_lab_enabled,
            duty_percent=profile.generator_lab_cpu_percent,
        ),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def command_local_dashboard(args: argparse.Namespace) -> int:
    from .dashboard import serve_dashboard

    print(f"Dashboard: http://{args.host}:{args.port}")
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print("Read-only dashboard is visible to the connected network.")
    try:
        serve_dashboard(args.profile, host=args.host, port=args.port)
    except KeyboardInterrupt:
        print("Dashboard stopped.")
        return 130
    return 0


def command_local_app(args: argparse.Namespace) -> int:
    import threading
    import webbrowser

    from .dashboard import create_dashboard_server

    server = create_dashboard_server(args.profile, host=args.host, port=args.port)
    dashboard = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.5},
        name="puzzleforge-dashboard",
        daemon=True,
    )
    dashboard.start()
    browser_host = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
    url = f"http://{browser_host}:{args.port}"
    print(f"PuzzleForge Local: {url}")
    if not args.no_open:
        try:
            webbrowser.open(url)
        except webbrowser.Error as exc:
            print(f"Browser did not open automatically: {exc}")
    try:
        return _run_local_campaign(args)
    finally:
        server.shutdown()
        dashboard.join(timeout=5)
        server.server_close()


def command_coordinator_init(args: argparse.Namespace) -> int:
    from .coordinator import Coordinator

    coordinator = Coordinator.initialize(
        args.database,
        puzzle_number=args.puzzle,
        chunk_size=args.chunk_size,
        seed=args.seed,
        planner_mode=args.mode,
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


def add_local_runtime_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--profile", type=Path, default=Path(".puzzleforge/local/profile.json")
    )
    parser.add_argument("--worker", default="local-gpu-0")
    parser.add_argument("--lease-seconds", type=positive_integer, default=3_600)
    parser.add_argument("--max-chunks", type=positive_integer)
    parser.add_argument("--engine-timeout", type=float)
    parser.add_argument(
        "--no-thermal-guard",
        action="store_true",
        help="disable NVIDIA temperature protection",
    )


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
    scan_parser.add_argument(
        "--batch-size",
        type=positive_integer,
        default=DEFAULT_BATCH_SIZE,
        help=(
            "keys per batched point block; one modular inversion is shared by "
            "the whole block"
        ),
    )
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

    cold_parser = subparsers.add_parser(
        "cold-preview",
        help="preview the least-searched keyspace bands and the cold work order",
    )
    cold_parser.add_argument("puzzle", type=int, choices=[p.number for p in puzzles()])
    cold_parser.add_argument("--chunk-size", type=positive_integer, default=1 << 32)
    cold_parser.add_argument("--seed", default="puzzleforge-cold-v1")
    cold_parser.add_argument("--preview", type=positive_integer, default=16)
    cold_parser.add_argument("--bands", type=positive_integer, default=4096)
    cold_parser.add_argument("--bands-preview", type=positive_integer, default=8)
    cold_parser.set_defaults(handler=command_cold_preview)

    hypothesis_parser = subparsers.add_parser(
        "hypothesis-preview",
        help="backtest Hypothesis Lab and preview its 10/90 range cycles",
    )
    hypothesis_parser.add_argument(
        "puzzle",
        type=int,
        choices=[p.number for p in puzzles() if p.status == "unsolved"],
    )
    hypothesis_parser.add_argument(
        "--chunk-size", type=positive_integer, default=1 << 40
    )
    hypothesis_parser.add_argument("--seed", default="puzzleforge-hypothesis-v1")
    hypothesis_parser.add_argument("--preview", type=positive_integer, default=18)
    hypothesis_parser.add_argument(
        "--top-models",
        type=positive_integer,
        default=12,
        help="number of ranked Model Zoo scores to include",
    )
    hypothesis_parser.set_defaults(handler=command_hypothesis_preview)

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

    local_setup_parser = subparsers.add_parser(
        "local-setup",
        help="validate, auto-tune, and create a resumable single-GPU campaign",
    )
    local_setup_parser.add_argument(
        "--state-dir", type=Path, default=Path(".puzzleforge/local")
    )
    local_setup_parser.add_argument("--binary", type=Path)
    local_setup_parser.add_argument("--device", type=nonnegative_integer, default=0)
    local_setup_parser.add_argument("--engine-timeout", type=float)
    local_setup_parser.add_argument(
        "--puzzle",
        type=int,
        choices=[p.number for p in puzzles() if p.status == "unsolved"],
        default=71,
    )
    local_setup_parser.add_argument(
        "--benchmark-profile", choices=("quick", "balanced", "full"), default="quick"
    )
    local_setup_parser.add_argument("--repeats", type=positive_integer, default=2)
    local_setup_parser.add_argument(
        "--benchmark-chunk-size", type=positive_integer, default=1 << 30
    )
    local_setup_parser.add_argument(
        "--chunk-seconds",
        type=positive_integer,
        default=300,
        help="target runtime per durable checkpoint chunk",
    )
    local_setup_parser.add_argument(
        "--mode",
        choices=("affine", "mosaic", "cold", "hypothesis"),
        default="hypothesis",
    )
    local_setup_parser.add_argument(
        "--seed",
        help="private work-order seed (random 256-bit value when omitted)",
    )
    local_setup_parser.add_argument("--max-temp", type=float, default=82.0)
    local_setup_parser.add_argument("--resume-temp", type=float, default=72.0)
    local_setup_parser.add_argument(
        "--thermal-poll-seconds", type=float, default=3.0
    )
    local_setup_parser.add_argument(
        "--thermal-retries", type=nonnegative_integer, default=3
    )
    local_setup_parser.set_defaults(handler=command_local_setup)

    local_run_parser = subparsers.add_parser(
        "local-run", help="run or resume the auto-tuned local GPU campaign"
    )
    add_local_runtime_arguments(local_run_parser)
    local_run_parser.set_defaults(handler=command_local_run)

    local_reseed_parser = subparsers.add_parser(
        "local-reseed",
        help="change the private work order without losing completed ranges",
    )
    local_reseed_parser.add_argument(
        "--profile", type=Path, default=Path(".puzzleforge/local/profile.json")
    )
    local_reseed_parser.add_argument(
        "--seed", help="explicit seed; omitted generates 256 random bits"
    )
    local_reseed_parser.set_defaults(handler=command_local_reseed)

    hypothesis_enable_parser = subparsers.add_parser(
        "hypothesis-enable",
        help="enable persistent 10/90 Hypothesis Lab on a local campaign",
    )
    hypothesis_enable_parser.add_argument(
        "--profile", type=Path, default=Path(".puzzleforge/local/profile.json")
    )
    hypothesis_enable_parser.set_defaults(handler=command_hypothesis_enable)

    generator_enable_parser = subparsers.add_parser(
        "generator-enable",
        help="enable challenge-scoped generator research beside the GPU scan",
    )
    generator_enable_parser.add_argument(
        "--profile", type=Path, default=Path(".puzzleforge/local/profile.json")
    )
    generator_enable_parser.add_argument(
        "--cpu-percent",
        type=positive_integer,
        default=10,
        help="duty cycle of one CPU core (1-50; GPU remains dedicated to BitCrack)",
    )
    generator_enable_parser.add_argument(
        "--wordlist",
        type=Path,
        help="optional challenge-specific seed phrase list",
    )
    generator_enable_parser.set_defaults(handler=command_generator_enable)

    local_sweep_parser = subparsers.add_parser(
        "local-sweep-configure",
        help="configure automatic sweep of a verified public-puzzle reward",
    )
    local_sweep_parser.add_argument("address", nargs="?")
    local_sweep_parser.add_argument(
        "--profile", type=Path, default=Path(".puzzleforge/local/profile.json")
    )
    local_sweep_parser.add_argument("--fee-floor", type=positive_integer, default=25)
    local_sweep_parser.add_argument("--fee-cap", type=positive_integer, default=500)
    local_sweep_parser.add_argument("--disable", action="store_true")
    local_sweep_parser.set_defaults(handler=command_local_sweep_configure)

    local_status_parser = subparsers.add_parser(
        "local-status", help="show local GPU tuning and exact durable progress"
    )
    local_status_parser.add_argument(
        "--profile", type=Path, default=Path(".puzzleforge/local/profile.json")
    )
    local_status_parser.set_defaults(handler=command_local_status)

    local_dashboard_parser = subparsers.add_parser(
        "local-dashboard", help="serve a lightweight read-only local GPU dashboard"
    )
    local_dashboard_parser.add_argument(
        "--profile", type=Path, default=Path(".puzzleforge/local/profile.json")
    )
    local_dashboard_parser.add_argument("--host", default="127.0.0.1")
    local_dashboard_parser.add_argument("--port", type=positive_integer, default=8788)
    local_dashboard_parser.set_defaults(handler=command_local_dashboard)

    local_app_parser = subparsers.add_parser(
        "local-app", help="run the local GPU campaign and dashboard together"
    )
    add_local_runtime_arguments(local_app_parser)
    local_app_parser.add_argument("--host", default="127.0.0.1")
    local_app_parser.add_argument("--port", type=positive_integer, default=8788)
    local_app_parser.add_argument("--no-open", action="store_true")
    local_app_parser.set_defaults(handler=command_local_app)

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
        "--mode",
        choices=("affine", "mosaic", "cold", "hypothesis"),
        default="affine",
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
