from __future__ import annotations

import argparse
import json
import os
import sys
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


def add_plan_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("puzzle", type=int, choices=[p.number for p in puzzles()])
    parser.add_argument("--chunk-size", type=positive_integer, default=100_000)
    parser.add_argument("--seed", default="puzzleforge-default")
    parser.add_argument("--shards", type=positive_integer, default=1)
    parser.add_argument("--shard-index", type=nonnegative_integer, default=0)


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
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        raise SystemExit(args.handler(args))
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
