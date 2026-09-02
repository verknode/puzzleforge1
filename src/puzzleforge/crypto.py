from __future__ import annotations

import hashlib
from functools import lru_cache
from typing import Final, Iterator, Sequence, TypeAlias


FIELD_P: Final = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
GROUP_N: Final = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
GENERATOR_X: Final = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
GENERATOR_Y: Final = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8

Point: TypeAlias = tuple[int, int] | None
GENERATOR: Final[Point] = (GENERATOR_X, GENERATOR_Y)

_BASE58_ALPHABET: Final = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_INDEX: Final = {char: index for index, char in enumerate(_BASE58_ALPHABET)}


def _inverse(value: int) -> int:
    return pow(value % FIELD_P, -1, FIELD_P)


def point_add(left: Point, right: Point) -> Point:
    if left is None:
        return right
    if right is None:
        return left

    x1, y1 = left
    x2, y2 = right
    if x1 == x2 and (y1 + y2) % FIELD_P == 0:
        return None

    if left == right:
        slope = (3 * x1 * x1) * _inverse(2 * y1)
    else:
        slope = (y2 - y1) * _inverse(x2 - x1)
    slope %= FIELD_P

    x3 = (slope * slope - x1 - x2) % FIELD_P
    y3 = (slope * (x1 - x3) - y1) % FIELD_P
    return x3, y3


def scalar_multiply(scalar: int, point: Point = GENERATOR) -> Point:
    if not 0 < scalar < GROUP_N:
        raise ValueError("private key must be in secp256k1 scalar range")

    result: Point = None
    addend = point
    value = scalar
    while value:
        if value & 1:
            result = point_add(result, addend)
        addend = point_add(addend, addend)
        value >>= 1
    return result


DEFAULT_BATCH_SIZE: Final = 1024


def batch_inverse(values: Sequence[int]) -> list[int]:
    """Invert every entry with a single modular inversion (Montgomery trick).

    Entries equal to zero have no inverse and are returned as zero so the
    caller can fall back to the generic addition path for that step.
    """

    count = len(values)
    prefixes = [0] * count
    running = 1
    for index, value in enumerate(values):
        prefixes[index] = running
        if value:
            running = running * value % FIELD_P

    inverse = pow(running, -1, FIELD_P)
    results = [0] * count
    for index in range(count - 1, -1, -1):
        value = values[index]
        if not value:
            continue
        results[index] = prefixes[index] * inverse % FIELD_P
        inverse = inverse * value % FIELD_P
    return results


@lru_cache(maxsize=8)
def generator_multiples(count: int) -> tuple[tuple[int, int], ...]:
    """Affine points ``1*G`` through ``count*G``."""

    if count < 1:
        raise ValueError("multiple count must be positive")
    table: list[tuple[int, int]] = [(GENERATOR_X, GENERATOR_Y)]
    for _ in range(count - 1):
        nxt = point_add(table[-1], GENERATOR)
        if nxt is None:
            raise ValueError("generator multiple table reached the point at infinity")
        table.append(nxt)
    return tuple(table)


def iter_sequential_points(
    start_scalar: int,
    count: int,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> Iterator[tuple[int, int]]:
    """Yield affine points for the consecutive scalars ``start_scalar ...``.

    One scalar multiplication starts the walk and each block of ``batch_size``
    keys costs a single modular inversion instead of one inversion per key.
    Every emitted point is identical to ``scalar_multiply`` for the same
    scalar; the batching changes cost, never the result.
    """

    if count < 1:
        return
    if batch_size < 1:
        raise ValueError("batch size must be positive")
    if not 0 < start_scalar <= GROUP_N - count:
        raise ValueError("sequential walk leaves the secp256k1 scalar range")

    width = batch_size if batch_size < count else count
    table = generator_multiples(width)
    current = scalar_multiply(start_scalar)
    emitted = 0

    while emitted < count:
        if current is None:
            raise ValueError("sequential walk reached the point at infinity")
        base_x, base_y = current
        remaining = count - emitted
        block = width if width < remaining else remaining
        advance = remaining > block
        steps = block - 1 + (1 if advance else 0)

        yield current
        emitted += 1
        if steps < 1:
            return

        inverses = batch_inverse(
            [(table[index][0] - base_x) % FIELD_P for index in range(steps)]
        )
        following: Point = None
        for index in range(steps):
            inverse = inverses[index]
            if inverse:
                addend_x, addend_y = table[index]
                slope = (addend_y - base_y) * inverse % FIELD_P
                x3 = (slope * slope - base_x - addend_x) % FIELD_P
                point = (x3, (slope * (base_x - x3) - base_y) % FIELD_P)
            else:
                point = point_add(current, table[index])
                if point is None:
                    raise ValueError("sequential walk reached the point at infinity")
            if index + 1 < block:
                yield point
                emitted += 1
            else:
                following = point
        if advance:
            current = following


def compressed_public_key(point: Point) -> bytes:
    if point is None:
        raise ValueError("point at infinity has no public-key encoding")
    x, y = point
    return bytes((2 | (y & 1),)) + x.to_bytes(32, "big")


def hash160(data: bytes) -> bytes:
    sha = hashlib.sha256(data).digest()
    try:
        return hashlib.new("ripemd160", sha).digest()
    except ValueError as exc:
        raise RuntimeError("this Python/OpenSSL build does not provide RIPEMD-160") from exc


def double_sha256(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def base58_decode(value: str) -> bytes:
    if not value:
        raise ValueError("empty Base58 value")
    number = 0
    for char in value:
        try:
            digit = _BASE58_INDEX[char]
        except KeyError as exc:
            raise ValueError(f"invalid Base58 character: {char!r}") from exc
        number = number * 58 + digit

    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading_zeroes = len(value) - len(value.lstrip("1"))
    return b"\x00" * leading_zeroes + raw


def base58_encode(raw: bytes) -> str:
    number = int.from_bytes(raw, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = _BASE58_ALPHABET[remainder] + encoded
    leading_zeroes = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * leading_zeroes + (encoded or ("" if leading_zeroes else "1"))


def decode_p2pkh(address: str) -> bytes:
    decoded = base58_decode(address)
    if len(decoded) != 25:
        raise ValueError("P2PKH Base58Check payload must be 25 bytes")
    payload, checksum = decoded[:-4], decoded[-4:]
    if double_sha256(payload)[:4] != checksum:
        raise ValueError("invalid Base58Check checksum")
    if payload[0] != 0x00:
        raise ValueError("only Bitcoin mainnet P2PKH addresses are supported")
    return payload[1:]


def p2pkh_address_from_point(point: Point) -> str:
    payload = b"\x00" + hash160(compressed_public_key(point))
    return base58_encode(payload + double_sha256(payload)[:4])


def p2pkh_address_from_private_key(private_key: int) -> str:
    return p2pkh_address_from_point(scalar_multiply(private_key))

