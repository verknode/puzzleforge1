from __future__ import annotations

import hashlib
from typing import Final, TypeAlias


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

