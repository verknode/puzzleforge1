#!/usr/bin/env python3
"""Termux research worker for the public Bitcoin Puzzle #71 challenge.

The program runs two disjoint, durable experiment lanes:

1. Numeric seed/generator hypotheses checked against solved public vectors.
2. Small integer recurrence hypotheses over normalized solved-key offsets.

Only a private key that independently derives the registered Puzzle #71
address is accepted.  This is challenge-specific research, not a general
wallet scanner.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import multiprocessing as mp
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable


SCHEMA = 1
TARGET_PUZZLE = 71
TARGET_ADDRESS = "1PWo3JeB9jrGwfHDNpdGK54CRas7fsVzXU"
CONTROL_ADDRESS = "1M92tSqNmQLYw33fuBvjmeadirh1ysMBxK"
CONTROL_KEY = 0xE0
SECP256K1_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
SECP256K1_GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
SECP256K1_GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
MASK64 = (1 << 64) - 1

KNOWN_KEYS_HEX = {
    1: "1", 2: "3", 3: "7", 4: "8", 5: "15", 6: "31", 7: "4c", 8: "e0",
    9: "1d3", 10: "202", 11: "483", 12: "a7b", 13: "1460", 14: "2930",
    15: "68f3", 16: "c936", 17: "1764f", 18: "3080d", 19: "5749f",
    20: "d2c55", 21: "1ba534", 22: "2de40f", 23: "556e52", 24: "dc2a04",
    25: "1fa5ee5", 26: "340326e", 27: "6ac3875", 28: "d916ce8",
    29: "17e2551e", 30: "3d94cd64", 31: "7d4fe747", 32: "b862a62e",
    33: "1a96ca8d8", 34: "34a65911d", 35: "4aed21170", 36: "9de820a7c",
    37: "1757756a93", 38: "22382facd0", 39: "4b5f8303e9", 40: "e9ae4933d6",
    41: "153869acc5b", 42: "2a221c58d8f", 43: "6bd3b27c591",
    44: "e02b35a358f", 45: "122fca143c05", 46: "2ec18388d544",
    47: "6cd610b53cba", 48: "ade6d7ce3b9b", 49: "174176b015f4d",
    50: "22bd43c2e9354", 51: "75070a1a009d4", 52: "efae164cb9e3c",
    53: "180788e47e326c", 54: "236fb6d5ad1f43", 55: "6abe1f9b67e114",
    56: "9d18b63ac4ffdf", 57: "1eb25c90795d61c", 58: "2c675b852189a21",
    59: "7496cbb87cab44f", 60: "fc07a1825367bbe", 61: "13c96a3742f64906",
    62: "363d541eb611abee", 63: "7cce5efdaccf6808", 64: "f7051f27b09112d4",
    65: "1a838b13505b26867", 66: "2832ed74f2b5e35ee", 67: "730fc235c1942c1ae",
    68: "bebb3940cd0fc1491", 69: "101d83275fb2bc7e0c", 70: "349b84b6431a6c4ef1",
}
KNOWN_KEYS = {number: int(value, 16) for number, value in KNOWN_KEYS_HEX.items()}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(payload, dict):
        raise ValueError(f"invalid state file: {path}")
    return payload


BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def base58check_decode(address: str) -> bytes:
    value = 0
    for character in address:
        try:
            digit = BASE58_ALPHABET.index(character)
        except ValueError as exc:
            raise ValueError("invalid Base58 address") from exc
        value = value * 58 + digit
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big") if value else b""
    raw = b"\x00" * (len(address) - len(address.lstrip("1"))) + raw
    if len(raw) != 25:
        raise ValueError("unexpected P2PKH address length")
    payload, checksum = raw[:-4], raw[-4:]
    expected = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    if not hmac.compare_digest(checksum, expected):
        raise ValueError("invalid P2PKH address checksum")
    if payload[0] != 0:
        raise ValueError("only Bitcoin mainnet P2PKH is supported")
    return payload[1:]


def _jacobian_double(point: tuple[int, int, int]) -> tuple[int, int, int]:
    x, y, z = point
    if y == 0 or z == 0:
        return 0, 1, 0
    yy = y * y % SECP256K1_P
    s = 4 * x * yy % SECP256K1_P
    m = 3 * x * x % SECP256K1_P
    nx = (m * m - 2 * s) % SECP256K1_P
    ny = (m * (s - nx) - 8 * yy * yy) % SECP256K1_P
    nz = 2 * y * z % SECP256K1_P
    return nx, ny, nz


def _jacobian_add(
    left: tuple[int, int, int], right: tuple[int, int, int]
) -> tuple[int, int, int]:
    x1, y1, z1 = left
    x2, y2, z2 = right
    if z1 == 0:
        return right
    if z2 == 0:
        return left
    z1z1 = z1 * z1 % SECP256K1_P
    z2z2 = z2 * z2 % SECP256K1_P
    u1 = x1 * z2z2 % SECP256K1_P
    u2 = x2 * z1z1 % SECP256K1_P
    s1 = y1 * z2 * z2z2 % SECP256K1_P
    s2 = y2 * z1 * z1z1 % SECP256K1_P
    if u1 == u2:
        return _jacobian_double(left) if s1 == s2 else (0, 1, 0)
    h_value = (u2 - u1) % SECP256K1_P
    i_value = (2 * h_value) ** 2 % SECP256K1_P
    j_value = h_value * i_value % SECP256K1_P
    r_value = 2 * (s2 - s1) % SECP256K1_P
    v_value = u1 * i_value % SECP256K1_P
    nx = (r_value * r_value - j_value - 2 * v_value) % SECP256K1_P
    ny = (r_value * (v_value - nx) - 2 * s1 * j_value) % SECP256K1_P
    nz = ((z1 + z2) ** 2 - z1z1 - z2z2) * h_value % SECP256K1_P
    return nx, ny, nz


def compressed_public_key(private_key: int) -> bytes:
    if not 1 <= private_key < SECP256K1_N:
        raise ValueError("private key is outside secp256k1")
    result = (0, 1, 0)
    addend = (SECP256K1_GX, SECP256K1_GY, 1)
    scalar = private_key
    while scalar:
        if scalar & 1:
            result = _jacobian_add(result, addend)
        addend = _jacobian_double(addend)
        scalar >>= 1
    x, y, z = result
    inverse = pow(z, SECP256K1_P - 2, SECP256K1_P)
    affine_x = x * inverse * inverse % SECP256K1_P
    affine_y = y * inverse * inverse * inverse % SECP256K1_P
    return bytes((2 | (affine_y & 1),)) + affine_x.to_bytes(32, "big")


def hash160(data: bytes) -> bytes:
    sha = hashlib.sha256(data).digest()
    try:
        return hashlib.new("ripemd160", sha).digest()
    except ValueError as exc:
        raise RuntimeError("RIPEMD160 is unavailable in this Python/OpenSSL build") from exc


TARGET_HASH160 = base58check_decode(TARGET_ADDRESS)
CONTROL_HASH160 = base58check_decode(CONTROL_ADDRESS)


def matches_target(private_key: int) -> bool:
    if not (1 << 70) <= private_key < (1 << 71):
        return False
    return hmac.compare_digest(
        hash160(compressed_public_key(private_key)), TARGET_HASH160
    )


def crypto_self_test() -> None:
    actual = hash160(compressed_public_key(CONTROL_KEY))
    if not hmac.compare_digest(actual, CONTROL_HASH160):
        raise RuntimeError("secp256k1/P2PKH self-test failed on solved puzzle #8")


def masked_key(raw_key: int, puzzle_number: int) -> int:
    width = puzzle_number - 1
    return (1 << width) | (raw_key & ((1 << width) - 1))


def _index(number: int, origin: int) -> int:
    return number - 1 + origin


def sha_direct(seed: bytes, _: int) -> int:
    return int.from_bytes(hashlib.sha256(seed).digest(), "big")


def sha_seed_u32_0(seed: bytes, number: int) -> int:
    suffix = _index(number, 0).to_bytes(4, "big")
    return int.from_bytes(hashlib.sha256(seed + suffix).digest(), "big")


def sha_seed_u32_1(seed: bytes, number: int) -> int:
    suffix = _index(number, 1).to_bytes(4, "big")
    return int.from_bytes(hashlib.sha256(seed + suffix).digest(), "big")


def sha_u32_seed(seed: bytes, number: int) -> int:
    prefix = _index(number, 0).to_bytes(4, "big")
    return int.from_bytes(hashlib.sha256(prefix + seed).digest(), "big")


def sha_seed_ascii_0(seed: bytes, number: int) -> int:
    suffix = str(_index(number, 0)).encode("ascii")
    return int.from_bytes(hashlib.sha256(seed + suffix).digest(), "big")


def sha_seed_ascii_1(seed: bytes, number: int) -> int:
    suffix = str(_index(number, 1)).encode("ascii")
    return int.from_bytes(hashlib.sha256(seed + suffix).digest(), "big")


def hmac_u32_0(seed: bytes, number: int) -> int:
    message = _index(number, 0).to_bytes(4, "big")
    return int.from_bytes(hmac.new(seed, message, hashlib.sha256).digest(), "big")


def hmac_u32_1(seed: bytes, number: int) -> int:
    message = _index(number, 1).to_bytes(4, "big")
    return int.from_bytes(hmac.new(seed, message, hashlib.sha256).digest(), "big")


def hash_chain(seed: bytes, number: int) -> int:
    value = seed
    for _ in range(_index(number, 0) + 1):
        value = hashlib.sha256(value).digest()
    return int.from_bytes(value, "big")


def python_mt(seed: bytes, number: int) -> int:
    generator = random.Random()
    generator.seed(seed, version=2)
    value = 0
    for _ in range(_index(number, 0) + 1):
        value = generator.getrandbits(256)
    return value


SCHEMES: tuple[tuple[str, Callable[[bytes, int], int]], ...] = (
    ("sha256(seed)", sha_direct),
    ("sha256(seed||u32be(index0))", sha_seed_u32_0),
    ("sha256(seed||u32be(index1))", sha_seed_u32_1),
    ("sha256(u32be(index0)||seed)", sha_u32_seed),
    ("sha256(seed||ascii(index0))", sha_seed_ascii_0),
    ("sha256(seed||ascii(index1))", sha_seed_ascii_1),
    ("hmac-sha256(seed,u32be(index0))", hmac_u32_0),
    ("hmac-sha256(seed,u32be(index1))", hmac_u32_1),
    ("sha256-hash-chain", hash_chain),
    ("python-mt19937", python_mt),
)


SEED_VARIANTS = 8


def seed_material(number: int, variant: int) -> tuple[str, bytes]:
    if number < 0 or not 0 <= variant < SEED_VARIANTS:
        raise ValueError("numeric seed coordinates are invalid")
    byte_length = max(1, (number.bit_length() + 7) // 8)
    fixed_length = max(8, byte_length)
    options = (
        ("decimal-ascii", str(number).encode("ascii")),
        ("hex-ascii", format(number, "x").encode("ascii")),
        ("minimal-big-endian", number.to_bytes(byte_length, "big")),
        ("minimal-little-endian", number.to_bytes(byte_length, "little")),
        ("fixed8-big-endian", number.to_bytes(fixed_length, "big")),
        ("fixed8-little-endian", number.to_bytes(fixed_length, "little")),
        ("key-prefix", f"key{number}".encode("ascii")),
        ("seed-prefix", f"seed{number}".encode("ascii")),
    )
    return options[variant]


def decode_seed_task(ordinal: int) -> tuple[int, int, int]:
    per_seed = SEED_VARIANTS * len(SCHEMES)
    number, remainder = divmod(ordinal, per_seed)
    variant, scheme = divmod(remainder, len(SCHEMES))
    return number, variant, scheme


def validate_generator(
    seed: bytes, scheme: Callable[[bytes, int], int]
) -> int | None:
    if masked_key(scheme(seed, 70), 70) != KNOWN_KEYS[70]:
        return None
    for number in range(65, 70):
        if masked_key(scheme(seed, number), number) != KNOWN_KEYS[number]:
            return None
    return masked_key(scheme(seed, TARGET_PUZZLE), TARGET_PUZZLE)


def zigzag_decode(value: int) -> int:
    return value // 2 if value % 2 == 0 else -(value // 2) - 1


def cantor_unpair(value: int) -> tuple[int, int]:
    diagonal = (math.isqrt(8 * value + 1) - 1) // 2
    triangle = diagonal * (diagonal + 1) // 2
    second = value - triangle
    first = diagonal - second
    return first, second


def coefficient_triple(ordinal: int) -> tuple[int, int, int]:
    pair, third = cantor_unpair(ordinal)
    first, second = cantor_unpair(pair)
    return zigzag_decode(first), zigzag_decode(second), zigzag_decode(third)


def normalized_offset(number: int) -> int:
    start = 1 << (number - 1)
    offset = KNOWN_KEYS[number] - start
    return (offset << 64) // start


NORMALIZED = {number: normalized_offset(number) for number in KNOWN_KEYS}


def recurrence_prediction(a_value: int, b_value: int, c_value: int, number: int) -> int:
    return (
        a_value * NORMALIZED[number - 1]
        + b_value * NORMALIZED[number - 2]
        + c_value * NORMALIZED[number - 3]
    ) & MASK64


def prefix_bits(left: int, right: int) -> int:
    difference = (left ^ right) & MASK64
    return 64 if difference == 0 else 64 - difference.bit_length()


def recurrence_candidate(
    a_value: int, b_value: int, c_value: int
) -> tuple[int, int, int]:
    scores = [
        prefix_bits(
            recurrence_prediction(a_value, b_value, c_value, number),
            NORMALIZED[number],
        )
        for number in range(63, 71)
    ]
    predicted = recurrence_prediction(a_value, b_value, c_value, 71)
    target_offset = (predicted * (1 << 70)) >> 64
    private_key = (1 << 70) | target_offset
    return private_key, sum(scores), min(scores)


def initial_worker_state(worker: int, workers: int) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "target_puzzle": TARGET_PUZZLE,
        "worker": worker,
        "workers": workers,
        "status": "ready",
        "seed_cursor": 0,
        "relation_cursor": 0,
        "generator_checks": 0,
        "relation_candidates": 0,
        "address_checks": 0,
        "exact_generator_filters": 0,
        "validated_generators": 0,
        "best_relation_score": 0,
        "best_relation_min_bits": 0,
        "best_relation": "",
        "started_at": utc_now(),
        "updated_at": utc_now(),
        "active_seconds": 0.0,
        "last_error": "",
    }


def load_worker_state(path: Path, worker: int, workers: int) -> dict[str, object]:
    state = read_json(path)
    if state is None:
        state = initial_worker_state(worker, workers)
        atomic_json(path, state)
        return state
    expected = (SCHEMA, TARGET_PUZZLE, worker, workers)
    actual = (
        int(state.get("schema", -1)),
        int(state.get("target_puzzle", -1)),
        int(state.get("worker", -1)),
        int(state.get("workers", -1)),
    )
    if actual != expected:
        raise ValueError(f"state layout mismatch in {path}")
    return state


def save_found(
    state_dir: Path,
    private_key: int,
    lane: str,
    detail: dict[str, object],
) -> None:
    if not matches_target(private_key):
        raise RuntimeError("internal error: candidate did not match Puzzle #71")
    payload = {
        "schema": SCHEMA,
        "puzzle": TARGET_PUZZLE,
        "address": TARGET_ADDRESS,
        "private_key_hex": f"{private_key:064x}",
        "lane": lane,
        "detail": detail,
        "verified_at": utc_now(),
    }
    found_path = state_dir / "FOUND-PUZZLE-71.json"
    atomic_json(found_path, payload)
    try:
        os.chmod(found_path, 0o600)
    except OSError:
        pass
    if shutil.which("termux-notification"):
        subprocess.run(
            [
                "termux-notification",
                "--title",
                "Puzzle #71",
                "--content",
                "Verified public-puzzle match found. Open FOUND-PUZZLE-71.json",
                "--priority",
                "max",
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def process_seed_task(
    global_ordinal: int,
    state: dict[str, object],
    state_dir: Path,
    stop_event,
) -> bool:
    number, variant, scheme_index = decode_seed_task(global_ordinal)
    variant_name, seed = seed_material(number, variant)
    scheme_name, scheme = SCHEMES[scheme_index]
    state["generator_checks"] = int(state["generator_checks"]) + 1
    if masked_key(scheme(seed, 70), 70) != KNOWN_KEYS[70]:
        return False
    state["exact_generator_filters"] = int(state["exact_generator_filters"]) + 1
    candidate = validate_generator(seed, scheme)
    if candidate is None:
        return False
    state["validated_generators"] = int(state["validated_generators"]) + 1
    state["address_checks"] = int(state["address_checks"]) + 1
    if not matches_target(candidate):
        return False
    save_found(
        state_dir,
        candidate,
        "numeric-seed-generator",
        {
            "numeric_seed": number,
            "seed_variant": variant_name,
            "scheme": scheme_name,
        },
    )
    stop_event.set()
    return True


def process_relation_task(
    global_ordinal: int,
    state: dict[str, object],
    state_dir: Path,
    stop_event,
) -> bool:
    a_value, b_value, c_value = coefficient_triple(global_ordinal)
    candidate, score, minimum = recurrence_candidate(a_value, b_value, c_value)
    state["relation_candidates"] = int(state["relation_candidates"]) + 1
    state["address_checks"] = int(state["address_checks"]) + 1
    if score > int(state["best_relation_score"]):
        state["best_relation_score"] = score
        state["best_relation_min_bits"] = minimum
        state["best_relation"] = f"a={a_value},b={b_value},c={c_value}"
    if not matches_target(candidate):
        return False
    save_found(
        state_dir,
        candidate,
        "normalized-integer-recurrence",
        {
            "a": a_value,
            "b": b_value,
            "c": c_value,
            "holdout_prefix_score": score,
            "minimum_holdout_prefix_bits": minimum,
        },
    )
    stop_event.set()
    return True


def worker_main(
    worker: int,
    workers: int,
    duty_percent: int,
    state_dir_text: str,
    stop_event,
) -> None:
    state_dir = Path(state_dir_text)
    state_path = state_dir / f"worker-{worker:02d}-of-{workers:02d}.json"
    try:
        crypto_self_test()
        state = load_worker_state(state_path, worker, workers)
        state["status"] = "running"
        state["last_error"] = ""
        atomic_json(state_path, state)
        window_seconds = 2.0
        save_deadline = time.monotonic() + 5.0
        lane = 0
        while not stop_event.is_set():
            window_started = time.monotonic()
            active_deadline = window_started + window_seconds * duty_percent / 100
            active_started = time.monotonic()
            while time.monotonic() < active_deadline and not stop_event.is_set():
                if lane == 0:
                    cursor = int(state["seed_cursor"])
                    ordinal = worker + cursor * workers
                    found = process_seed_task(
                        ordinal, state, state_dir, stop_event
                    )
                    state["seed_cursor"] = cursor + 1
                else:
                    cursor = int(state["relation_cursor"])
                    ordinal = worker + cursor * workers
                    found = process_relation_task(
                        ordinal, state, state_dir, stop_event
                    )
                    state["relation_cursor"] = cursor + 1
                lane ^= 1
                if found:
                    state["status"] = "found"
                    break
                if time.monotonic() >= save_deadline:
                    state["updated_at"] = utc_now()
                    atomic_json(state_path, state)
                    save_deadline = time.monotonic() + 5.0
            state["active_seconds"] = float(state["active_seconds"]) + max(
                0.0, time.monotonic() - active_started
            )
            state["updated_at"] = utc_now()
            atomic_json(state_path, state)
            remaining = window_seconds - (time.monotonic() - window_started)
            if remaining > 0:
                stop_event.wait(remaining)
        if state["status"] != "found":
            state["status"] = "stopped"
        state["updated_at"] = utc_now()
        atomic_json(state_path, state)
    except BaseException as exc:
        try:
            state = load_worker_state(state_path, worker, workers)
            state["status"] = "error"
            state["last_error"] = " ".join(str(exc).split())[:2000]
            state["updated_at"] = utc_now()
            atomic_json(state_path, state)
        finally:
            stop_event.set()


def ensure_manifest(state_dir: Path, workers: int) -> None:
    manifest_path = state_dir / "manifest.json"
    manifest = read_json(manifest_path)
    expected = {
        "schema": SCHEMA,
        "target_puzzle": TARGET_PUZZLE,
        "target_address": TARGET_ADDRESS,
        "workers": workers,
    }
    if manifest is None:
        payload = dict(expected)
        payload["created_at"] = utc_now()
        atomic_json(manifest_path, payload)
        return
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise ValueError(
                f"{manifest_path} was created for another layout; "
                "use a different --state-dir"
            )


def aggregate_status(state_dir: Path, workers: int) -> dict[str, object]:
    states = [
        read_json(state_dir / f"worker-{worker:02d}-of-{workers:02d}.json")
        for worker in range(workers)
    ]
    existing = [state for state in states if state is not None]
    best = max(existing, key=lambda value: int(value["best_relation_score"]), default=None)
    return {
        "generator_checks": sum(int(value["generator_checks"]) for value in existing),
        "relation_candidates": sum(int(value["relation_candidates"]) for value in existing),
        "address_checks": sum(int(value["address_checks"]) for value in existing),
        "best_score": 0 if best is None else int(best["best_relation_score"]),
        "best_relation": "" if best is None else str(best["best_relation"]),
        "errors": [
            str(value["last_error"])
            for value in existing
            if value.get("status") == "error"
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Durable Termux research worker for public Bitcoin Puzzle #71"
    )
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--duty", type=int, default=50)
    parser.add_argument("--report-seconds", type=int, default=30)
    parser.add_argument("--state-dir", type=Path, default=Path(".puzzle71-phone"))
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument(
        "--run-seconds",
        type=float,
        help="optional bounded run for testing; omit for continuous operation",
    )
    args = parser.parse_args()
    if not 1 <= args.workers <= 8:
        parser.error("--workers must be 1-8")
    if not 5 <= args.duty <= 100:
        parser.error("--duty must be 5-100")
    if not 5 <= args.report_seconds <= 3600:
        parser.error("--report-seconds must be 5-3600")
    if args.run_seconds is not None and args.run_seconds <= 0:
        parser.error("--run-seconds must be positive")
    return args


def main() -> int:
    args = parse_args()
    crypto_self_test()
    if args.self_test:
        print("PASS: secp256k1/P2PKH verified on solved puzzle #8")
        print(f"TARGET: Puzzle #71 {TARGET_ADDRESS}")
        return 0

    state_dir = args.state_dir.expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    ensure_manifest(state_dir, args.workers)
    context = mp.get_context("spawn")
    stop_event = context.Event()

    def request_stop(_signum, _frame) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    processes = [
        context.Process(
            target=worker_main,
            args=(
                worker,
                args.workers,
                args.duty,
                str(state_dir),
                stop_event,
            ),
            name=f"puzzle71-phone-{worker}",
        )
        for worker in range(args.workers)
    ]
    for process in processes:
        process.start()

    baseline = aggregate_status(state_dir, args.workers)
    print(
        f"RUNNING Puzzle #71 | workers={args.workers} | "
        f"duty={args.duty}% each | state={state_dir}",
        flush=True,
    )
    started = time.monotonic()
    try:
        while not stop_event.wait(args.report_seconds):
            status = aggregate_status(state_dir, args.workers)
            elapsed = max(0.001, time.monotonic() - started)
            new_address_checks = int(status["address_checks"]) - int(
                baseline["address_checks"]
            )
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] "
                f"seed/gen={status['generator_checks']:,} | "
                f"relations={status['relation_candidates']:,} | "
                f"address checks={status['address_checks']:,} | "
                f"rate={new_address_checks / elapsed:,.1f}/s | "
                f"best={status['best_score']} bits "
                f"({status['best_relation'] or 'pending'})",
                flush=True,
            )
            if status["errors"]:
                print(f"ERROR: {status['errors'][0]}", file=sys.stderr, flush=True)
                stop_event.set()
            if args.run_seconds is not None and elapsed >= args.run_seconds:
                stop_event.set()
    finally:
        stop_event.set()
        for process in processes:
            process.join(timeout=10)
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    found_path = state_dir / "FOUND-PUZZLE-71.json"
    if found_path.is_file():
        print(f"MATCH VERIFIED: {found_path}", flush=True)
        return 0
    status = aggregate_status(state_dir, args.workers)
    if status["errors"]:
        print(f"STOPPED WITH ERROR: {status['errors'][0]}", file=sys.stderr)
        return 1
    print("STOPPED. Progress saved; the next launch resumes without repeats.")
    return 0


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
