from __future__ import annotations

import json
import math
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils

from .crypto import (
    GROUP_N,
    compressed_public_key,
    decode_p2pkh,
    double_sha256,
    p2pkh_address_from_private_key,
    scalar_multiply,
)


SIGHASH_ALL = 1
RBF_SEQUENCE = 0xFFFFFFFD
DEFAULT_ESPLORA_ENDPOINTS = (
    "https://mempool.space/api",
    "https://blockstream.info/api",
)

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_INDEX = {char: index for index, char in enumerate(_BECH32_CHARSET)}


class SweepError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class UTXO:
    txid: str
    vout: int
    value: int
    confirmed: bool = True

    def __post_init__(self) -> None:
        try:
            raw = bytes.fromhex(self.txid)
        except ValueError as exc:
            raise ValueError("UTXO txid must be hexadecimal") from exc
        if len(raw) != 32:
            raise ValueError("UTXO txid must be 32 bytes")
        if isinstance(self.vout, bool) or not 0 <= self.vout <= 0xFFFFFFFF:
            raise ValueError("UTXO output index is invalid")
        if isinstance(self.value, bool) or self.value <= 0:
            raise ValueError("UTXO value must be positive")


@dataclass(frozen=True, slots=True)
class SignedSweep:
    source_address: str
    destination_address: str
    raw_transaction_hex: str
    txid: str
    input_count: int
    input_value_sats: int
    output_value_sats: int
    fee_sats: int
    fee_rate_sat_vb: int
    virtual_size: int


@dataclass(frozen=True, slots=True)
class SweepReceipt:
    state: str
    destination_address: str
    txid: str | None = None
    output_value_sats: int | None = None
    fee_sats: int | None = None
    detail: str = ""

    @property
    def broadcast(self) -> bool:
        return self.state == "broadcast"


class SweepNetwork(Protocol):
    def confirmed_utxos(self, address: str) -> tuple[UTXO, ...]: ...

    def recommended_fee_rate(self, floor: int, cap: int) -> int: ...

    def broadcast(self, raw_transaction_hex: str, expected_txid: str) -> tuple[str, ...]: ...


class EsploraNetwork:
    """Small HTTPS-only client for public Bitcoin chain data and broadcasting."""

    def __init__(
        self,
        endpoints: Sequence[str] = DEFAULT_ESPLORA_ENDPOINTS,
        *,
        timeout_seconds: float = 12.0,
    ) -> None:
        cleaned = tuple(endpoint.rstrip("/") for endpoint in endpoints)
        if not cleaned or any(not endpoint.startswith("https://") for endpoint in cleaned):
            raise ValueError("Esplora endpoints must use HTTPS")
        self.endpoints = cleaned
        self.timeout_seconds = float(timeout_seconds)

    def confirmed_utxos(self, address: str) -> tuple[UTXO, ...]:
        answers: list[tuple[UTXO, ...]] = []
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=len(self.endpoints)) as executor:
            futures = {
                executor.submit(self._confirmed_utxos_at, endpoint, address): endpoint
                for endpoint in self.endpoints
            }
            for future in as_completed(futures):
                endpoint = futures[future]
                try:
                    answers.append(future.result())
                except (
                    HTTPError,
                    URLError,
                    OSError,
                    KeyError,
                    TypeError,
                    ValueError,
                    SweepError,
                ) as exc:
                    errors.append(f"{endpoint}: {exc}")

        if not answers:
            raise SweepError("could not obtain confirmed UTXOs: " + "; ".join(errors))
        first = answers[0]
        if any(answer != first for answer in answers[1:]):
            raise SweepError("independent explorers disagree about the source UTXOs")
        if not first:
            raise SweepError("the puzzle address has no confirmed unspent outputs")
        return first

    def _confirmed_utxos_at(self, endpoint: str, address: str) -> tuple[UTXO, ...]:
        payload = self._json_get(f"{endpoint}/address/{address}/utxo")
        if not isinstance(payload, list):
            raise SweepError("UTXO response is not a list")
        parsed: list[UTXO] = []
        for item in payload:
            if not isinstance(item, dict):
                raise SweepError("UTXO entry is not an object")
            status = item.get("status")
            confirmed = isinstance(status, dict) and status.get("confirmed") is True
            if confirmed:
                parsed.append(
                    UTXO(
                        txid=str(item["txid"]),
                        vout=int(item["vout"]),
                        value=int(item["value"]),
                        confirmed=True,
                    )
                )
        return tuple(sorted(parsed, key=lambda item: (item.txid, item.vout)))

    def recommended_fee_rate(self, floor: int, cap: int) -> int:
        if not 1 <= floor <= cap <= 10_000:
            raise ValueError("invalid sweep fee bounds")
        estimates: list[float] = []
        with ThreadPoolExecutor(max_workers=len(self.endpoints)) as executor:
            futures = {
                executor.submit(self._fee_estimate_at, endpoint): endpoint
                for endpoint in self.endpoints
            }
            for future in as_completed(futures):
                try:
                    value = future.result()
                except (HTTPError, URLError, OSError, KeyError, TypeError, ValueError):
                    continue
                if value is not None:
                    estimates.append(value)
        finite = [value for value in estimates if math.isfinite(value) and value > 0]
        estimated = max(finite, default=float(floor))
        return min(cap, max(floor, math.ceil(estimated)))

    def _fee_estimate_at(self, endpoint: str) -> float | None:
        if "mempool.space" in endpoint:
            payload = self._json_get(f"{endpoint}/v1/fees/recommended")
            if isinstance(payload, dict):
                return float(payload["fastestFee"])
            return None
        payload = self._json_get(f"{endpoint}/fee-estimates")
        if isinstance(payload, dict):
            value = payload.get("1", payload.get(1))
            if value is not None:
                return float(value)
        return None

    def broadcast(self, raw_transaction_hex: str, expected_txid: str) -> tuple[str, ...]:
        accepted: list[str] = []
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=len(self.endpoints)) as executor:
            futures = {
                executor.submit(
                    self._broadcast_at,
                    endpoint,
                    raw_transaction_hex,
                    expected_txid,
                ): endpoint
                for endpoint in self.endpoints
            }
            for future in as_completed(futures):
                endpoint = futures[future]
                try:
                    if future.result():
                        accepted.append(endpoint)
                except (HTTPError, URLError, OSError, SweepError) as exc:
                    errors.append(f"{endpoint}: {exc}")
        if not accepted:
            raise SweepError("no broadcaster accepted the transaction: " + "; ".join(errors))
        return tuple(sorted(accepted))

    def _broadcast_at(
        self,
        endpoint: str,
        raw_transaction_hex: str,
        expected_txid: str,
    ) -> bool:
        body = raw_transaction_hex.encode("ascii")
        request = Request(
            f"{endpoint}/tx",
            data=body,
            method="POST",
            headers={
                "Content-Type": "text/plain",
                "User-Agent": "PuzzleForge/0.11",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                returned = response.read().decode("ascii", errors="replace").strip()
            if returned != expected_txid:
                raise SweepError(f"broadcaster returned unexpected txid {returned!r}")
            return True
        except (HTTPError, URLError, OSError, SweepError):
            # A previous POST may have reached the node before the connection
            # failed. Treat an independently queryable mempool/chain entry as
            # success so restart remains idempotent.
            try:
                status = self._json_get(f"{endpoint}/tx/{expected_txid}/status")
            except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError):
                raise
            if not isinstance(status, dict) or "confirmed" not in status:
                raise SweepError("broadcaster did not return a valid transaction status")
            return True

    def _json_get(self, url: str) -> object:
        request = Request(url, headers={"User-Agent": "PuzzleForge/0.11"})
        with urlopen(request, timeout=self.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))


def decode_mainnet_p2wpkh(address: str) -> bytes:
    """Validate a bc1q P2WPKH address and return its 20-byte witness program."""

    if not isinstance(address, str) or not address:
        raise ValueError("sweep destination address is empty")
    if address.lower() != address and address.upper() != address:
        raise ValueError("Bech32 address must not mix upper and lower case")
    normalized = address.lower()
    separator = normalized.rfind("1")
    if separator < 1 or separator + 7 > len(normalized) or len(normalized) > 90:
        raise ValueError("invalid Bech32 address length or separator")
    hrp = normalized[:separator]
    if hrp != "bc":
        raise ValueError("only Bitcoin mainnet addresses are accepted")
    try:
        data = [_BECH32_INDEX[char] for char in normalized[separator + 1 :]]
    except KeyError as exc:
        raise ValueError("invalid Bech32 character") from exc
    if _bech32_polymod(_bech32_hrp_expand(hrp) + data) != 1:
        raise ValueError("invalid Bech32 checksum")
    payload = data[:-6]
    if not payload or payload[0] != 0:
        raise ValueError("only witness version 0 Native SegWit is accepted")
    program = bytes(_convert_bits(payload[1:], 5, 8, pad=False))
    if len(program) != 20:
        raise ValueError("only bc1q P2WPKH destinations are accepted")
    return program


def build_sweep_transaction(
    private_key: int,
    source_address: str,
    destination_address: str,
    utxos: Sequence[UTXO],
    fee_rate_sat_vb: int,
) -> SignedSweep:
    if p2pkh_address_from_private_key(private_key) != source_address:
        raise SweepError("private key does not match the reviewed puzzle address")
    destination_program = decode_mainnet_p2wpkh(destination_address)
    selected = tuple(
        sorted(
            (item for item in utxos if item.confirmed),
            key=lambda item: (item.txid, item.vout),
        )
    )
    if not selected:
        raise SweepError("no confirmed UTXOs are available to sweep")
    if isinstance(fee_rate_sat_vb, bool) or not 1 <= fee_rate_sat_vb <= 10_000:
        raise ValueError("fee rate must be 1-10000 sat/vB")

    total = sum(item.value for item in selected)
    # All source inputs are legacy compressed-P2PKH. Budget the maximum usual
    # DER signature size plus two bytes of safety so the actual feerate never
    # falls below the configured target.
    estimated_vsize = 43 + 149 * len(selected)
    fee = fee_rate_sat_vb * estimated_vsize
    output_value = total - fee
    if output_value <= 294:
        raise SweepError("sweep output would be dust after the transaction fee")

    source_hash160 = decode_p2pkh(source_address)
    source_script = b"\x76\xa9\x14" + source_hash160 + b"\x88\xac"
    destination_script = b"\x00\x14" + destination_program
    public_key = compressed_public_key(scalar_multiply(private_key))
    cryptography_key = ec.derive_private_key(private_key, ec.SECP256K1())

    scripts = [b"" for _ in selected]
    for index in range(len(selected)):
        digest = _signature_hash(
            selected,
            scripts,
            index,
            source_script,
            output_value,
            destination_script,
        )
        der = cryptography_key.sign(
            digest,
            ec.ECDSA(utils.Prehashed(hashes.SHA256())),
        )
        r, s = utils.decode_dss_signature(der)
        if s > GROUP_N // 2:
            s = GROUP_N - s
        signature = utils.encode_dss_signature(r, s) + bytes((SIGHASH_ALL,))
        scripts[index] = _push_data(signature) + _push_data(public_key)

    raw = _serialize_transaction(selected, scripts, output_value, destination_script)
    actual_vsize = len(raw)
    actual_fee = total - output_value
    if actual_fee < fee_rate_sat_vb * actual_vsize:
        raise SweepError("signed transaction fee rate is below the configured target")
    txid = double_sha256(raw)[::-1].hex()
    return SignedSweep(
        source_address=source_address,
        destination_address=destination_address,
        raw_transaction_hex=raw.hex(),
        txid=txid,
        input_count=len(selected),
        input_value_sats=total,
        output_value_sats=output_value,
        fee_sats=actual_fee,
        fee_rate_sat_vb=fee_rate_sat_vb,
        virtual_size=actual_vsize,
    )


def execute_sweep(
    private_key: int,
    source_address: str,
    destination_address: str,
    record_path: Path,
    *,
    fee_floor: int = 25,
    fee_cap: int = 500,
    network: SweepNetwork | None = None,
) -> SweepReceipt:
    network = EsploraNetwork() if network is None else network
    existing = load_sweep_record(record_path)
    if existing and existing.get("destination_address") != destination_address:
        raise SweepError("pending sweep destination differs from the configured address")
    if existing and existing.get("source_address") != source_address:
        raise SweepError("pending sweep source differs from the reviewed puzzle address")
    if existing and existing.get("state") == "broadcast":
        return _receipt_from_record(existing)

    signed: SignedSweep
    if existing and existing.get("raw_transaction_hex") and existing.get("txid"):
        signed = _signed_from_record(existing)
    else:
        utxos = network.confirmed_utxos(source_address)
        fee_rate = network.recommended_fee_rate(fee_floor, fee_cap)
        signed = build_sweep_transaction(
            private_key,
            source_address,
            destination_address,
            utxos,
            fee_rate,
        )
        save_sweep_record(record_path, signed, state="pending")

    try:
        accepted_by = network.broadcast(signed.raw_transaction_hex, signed.txid)
    except SweepError as exc:
        save_sweep_record(record_path, signed, state="pending", detail=str(exc))
        return SweepReceipt(
            state="pending",
            destination_address=signed.destination_address,
            txid=signed.txid,
            output_value_sats=signed.output_value_sats,
            fee_sats=signed.fee_sats,
            detail=str(exc),
        )

    detail = "accepted by " + ", ".join(accepted_by)
    save_sweep_record(record_path, signed, state="broadcast", detail=detail)
    return SweepReceipt(
        state="broadcast",
        destination_address=signed.destination_address,
        txid=signed.txid,
        output_value_sats=signed.output_value_sats,
        fee_sats=signed.fee_sats,
        detail=detail,
    )


def load_sweep_record(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise SweepError(f"invalid sweep record: {exc}") from exc
    if not isinstance(payload, dict):
        raise SweepError("sweep record must be a JSON object")
    return payload


def save_sweep_record(
    path: Path,
    signed: SignedSweep,
    *,
    state: str,
    detail: str = "",
) -> None:
    if state not in {"pending", "broadcast"}:
        raise ValueError("invalid sweep state")
    payload: dict[str, object] = asdict(signed)
    payload.update(
        {
            "schema": 1,
            "state": state,
            "detail": detail,
            "updated_at": datetime.now(UTC).isoformat(),
        }
    )
    _save_json_atomic(path, payload)


def _receipt_from_record(payload: dict[str, object]) -> SweepReceipt:
    return SweepReceipt(
        state=str(payload["state"]),
        destination_address=str(payload["destination_address"]),
        txid=str(payload["txid"]),
        output_value_sats=int(payload["output_value_sats"]),
        fee_sats=int(payload["fee_sats"]),
        detail=str(payload.get("detail", "")),
    )


def _signed_from_record(payload: dict[str, object]) -> SignedSweep:
    try:
        signed = SignedSweep(
            source_address=str(payload["source_address"]),
            destination_address=str(payload["destination_address"]),
            raw_transaction_hex=str(payload["raw_transaction_hex"]),
            txid=str(payload["txid"]),
            input_count=int(payload["input_count"]),
            input_value_sats=int(payload["input_value_sats"]),
            output_value_sats=int(payload["output_value_sats"]),
            fee_sats=int(payload["fee_sats"]),
            fee_rate_sat_vb=int(payload["fee_rate_sat_vb"]),
            virtual_size=int(payload["virtual_size"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SweepError(f"invalid pending sweep record: {exc}") from exc
    raw = bytes.fromhex(signed.raw_transaction_hex)
    if double_sha256(raw)[::-1].hex() != signed.txid:
        raise SweepError("pending sweep transaction does not match its recorded txid")
    return signed


def _signature_hash(
    utxos: Sequence[UTXO],
    scripts: Sequence[bytes],
    signing_index: int,
    source_script: bytes,
    output_value: int,
    destination_script: bytes,
) -> bytes:
    signing_scripts = [b"" for _ in scripts]
    signing_scripts[signing_index] = source_script
    preimage = _serialize_transaction(
        utxos,
        signing_scripts,
        output_value,
        destination_script,
    ) + SIGHASH_ALL.to_bytes(4, "little")
    return double_sha256(preimage)


def _serialize_transaction(
    utxos: Sequence[UTXO],
    scripts: Sequence[bytes],
    output_value: int,
    destination_script: bytes,
) -> bytes:
    if len(utxos) != len(scripts):
        raise ValueError("transaction input/script count mismatch")
    raw = bytearray((2).to_bytes(4, "little"))
    raw.extend(_varint(len(utxos)))
    for utxo, script_sig in zip(utxos, scripts, strict=True):
        raw.extend(bytes.fromhex(utxo.txid)[::-1])
        raw.extend(utxo.vout.to_bytes(4, "little"))
        raw.extend(_varint(len(script_sig)))
        raw.extend(script_sig)
        raw.extend(RBF_SEQUENCE.to_bytes(4, "little"))
    raw.extend(_varint(1))
    raw.extend(output_value.to_bytes(8, "little"))
    raw.extend(_varint(len(destination_script)))
    raw.extend(destination_script)
    raw.extend((0).to_bytes(4, "little"))
    return bytes(raw)


def _push_data(data: bytes) -> bytes:
    if len(data) > 75:
        raise ValueError("only direct small pushes are supported")
    return bytes((len(data),)) + data


def _varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varint cannot be negative")
    if value < 0xFD:
        return bytes((value,))
    if value <= 0xFFFF:
        return b"\xfd" + value.to_bytes(2, "little")
    if value <= 0xFFFFFFFF:
        return b"\xfe" + value.to_bytes(4, "little")
    return b"\xff" + value.to_bytes(8, "little")


def _bech32_polymod(values: Sequence[int]) -> int:
    generators = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for index, generator in enumerate(generators):
            if (top >> index) & 1:
                checksum ^= generator
    return checksum


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(char) >> 5 for char in hrp] + [0] + [ord(char) & 31 for char in hrp]


def _convert_bits(
    values: Sequence[int],
    from_bits: int,
    to_bits: int,
    *,
    pad: bool,
) -> list[int]:
    accumulator = 0
    bit_count = 0
    result: list[int] = []
    maximum = (1 << to_bits) - 1
    for value in values:
        if value < 0 or value >> from_bits:
            raise ValueError("invalid data while converting Bech32 bits")
        accumulator = (accumulator << from_bits) | value
        bit_count += from_bits
        while bit_count >= to_bits:
            bit_count -= to_bits
            result.append((accumulator >> bit_count) & maximum)
    if pad:
        if bit_count:
            result.append((accumulator << (to_bits - bit_count)) & maximum)
    elif bit_count >= from_bits or ((accumulator << (to_bits - bit_count)) & maximum):
        raise ValueError("invalid Bech32 padding")
    return result


def _save_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path = path.expanduser().resolve()
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
