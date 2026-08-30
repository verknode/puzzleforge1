import json
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils

from puzzleforge.coordinator import Coordinator
from puzzleforge.crypto import GROUP_N, decode_p2pkh
from puzzleforge.engine import EngineOutcome, EngineTuning
from puzzleforge.local import LocalProfile, run_local_once
from puzzleforge.registry import get_puzzle
from puzzleforge.sweep import (
    SIGHASH_ALL,
    SignedSweep,
    SweepError,
    UTXO,
    _signature_hash,
    build_sweep_transaction,
    decode_mainnet_p2wpkh,
    execute_sweep,
)


# BIP173's public mainnet P2WPKH test vector. Real destinations belong only in
# the untracked local profile, never in repository tests.
DESTINATION = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
FAKE_TXID = "11" * 32


class FakeNetwork:
    def __init__(self, *, reject: bool = False) -> None:
        self.reject = reject
        self.utxo_calls = 0
        self.broadcast_calls = 0

    def confirmed_utxos(self, address: str) -> tuple[UTXO, ...]:
        self.utxo_calls += 1
        return (UTXO(FAKE_TXID, 1, 1_000_000),)

    def recommended_fee_rate(self, floor: int, cap: int) -> int:
        return min(cap, max(floor, 40))

    def broadcast(self, raw_transaction_hex: str, expected_txid: str) -> tuple[str, ...]:
        self.broadcast_calls += 1
        if self.reject:
            raise SweepError("simulated network outage")
        self.asserted_txid = expected_txid
        self.asserted_raw = raw_transaction_hex
        return ("https://example.invalid/api",)


class FoundPuzzle8Engine:
    def scan(self, puzzle, chunk):
        return EngineOutcome(
            status="found",
            checked=97,
            elapsed_seconds=0.1,
            rate_keys_per_second=1_000.0,
            message="found",
            found_key=0xE0,
        )


class SweepTests(unittest.TestCase):
    def test_configured_destination_is_valid_mainnet_native_segwit(self) -> None:
        self.assertEqual(
            decode_mainnet_p2wpkh(DESTINATION).hex(),
            "751e76e8199196d454941c45d1b3a323f1433bd6",
        )

    def test_bad_destination_checksum_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "checksum"):
            decode_mainnet_p2wpkh(DESTINATION[:-1] + "q")

    def test_signed_sweep_has_valid_low_s_bitcoin_signature(self) -> None:
        puzzle = get_puzzle(8)
        utxo = UTXO(FAKE_TXID, 1, 1_000_000)
        signed = build_sweep_transaction(
            0xE0,
            puzzle.address,
            DESTINATION,
            (utxo,),
            40,
        )
        parsed = self._parse_single_input(signed)
        signature_with_type = parsed["signature"]
        self.assertEqual(signature_with_type[-1], SIGHASH_ALL)
        der = signature_with_type[:-1]
        _, s = utils.decode_dss_signature(der)
        self.assertLessEqual(s, GROUP_N // 2)
        source_hash = decode_p2pkh(puzzle.address)
        source_script = b"\x76\xa9\x14" + source_hash + b"\x88\xac"
        destination_script = b"\x00\x14" + decode_mainnet_p2wpkh(DESTINATION)
        digest = _signature_hash(
            (utxo,),
            (b"",),
            0,
            source_script,
            signed.output_value_sats,
            destination_script,
        )
        public_key = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256K1(), parsed["public_key"]
        )
        public_key.verify(
            der,
            digest,
            ec.ECDSA(utils.Prehashed(hashes.SHA256())),
        )
        self.assertEqual(parsed["output_value"], signed.output_value_sats)
        self.assertEqual(parsed["destination_script"], destination_script)
        self.assertGreaterEqual(signed.fee_sats, 40 * signed.virtual_size)

    def test_every_input_in_a_multi_utxo_sweep_has_a_valid_signature(self) -> None:
        puzzle = get_puzzle(8)
        utxos = (
            UTXO("11" * 32, 1, 1_000_000),
            UTXO("22" * 32, 3, 50_000),
        )
        signed = build_sweep_transaction(
            0xE0,
            puzzle.address,
            DESTINATION,
            utxos,
            40,
        )
        parsed = self._parse_transaction(signed)
        source_hash = decode_p2pkh(puzzle.address)
        source_script = b"\x76\xa9\x14" + source_hash + b"\x88\xac"
        destination_script = b"\x00\x14" + decode_mainnet_p2wpkh(DESTINATION)

        self.assertEqual(len(parsed["inputs"]), 2)
        for index, transaction_input in enumerate(parsed["inputs"]):
            signature_with_type = transaction_input["signature"]
            der = signature_with_type[:-1]
            self.assertEqual(signature_with_type[-1], SIGHASH_ALL)
            _, s = utils.decode_dss_signature(der)
            self.assertLessEqual(s, GROUP_N // 2)
            digest = _signature_hash(
                utxos,
                (b"", b""),
                index,
                source_script,
                signed.output_value_sats,
                destination_script,
            )
            public_key = ec.EllipticCurvePublicKey.from_encoded_point(
                ec.SECP256K1(), transaction_input["public_key"]
            )
            public_key.verify(
                der,
                digest,
                ec.ECDSA(utils.Prehashed(hashes.SHA256())),
            )

        self.assertEqual(parsed["output_value"], signed.output_value_sats)
        self.assertEqual(parsed["destination_script"], destination_script)

    def test_pending_transaction_is_durable_and_rebroadcast_without_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sweep.json"
            rejected = FakeNetwork(reject=True)
            first = execute_sweep(
                0xE0,
                get_puzzle(8).address,
                DESTINATION,
                path,
                network=rejected,
            )
            accepted = FakeNetwork()
            second = execute_sweep(
                0xE0,
                get_puzzle(8).address,
                DESTINATION,
                path,
                network=accepted,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(first.state, "pending")
        self.assertEqual(second.state, "broadcast")
        self.assertEqual(accepted.utxo_calls, 0)
        self.assertEqual(accepted.broadcast_calls, 1)
        self.assertEqual(payload["state"], "broadcast")
        self.assertEqual(payload["destination_address"], DESTINATION)

    def test_local_match_broadcasts_then_scrubs_plaintext_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "campaign.sqlite3"
            Coordinator.initialize(
                database,
                puzzle_number=8,
                chunk_size=128,
                seed="sweep-test",
            )
            profile = LocalProfile(
                schema=1,
                puzzle=8,
                binary=str(root / "fake-cuBitCrack"),
                tuning=EngineTuning(device=0),
                measured_rate_keys_per_second=1_000.0,
                benchmark_relative_spread=0.0,
                chunk_size=128,
                target_chunk_seconds=300,
                planner_mode="affine",
                seed="sweep-test",
                database=str(database),
                benchmark_report=str(root / "benchmark.json"),
                device_probe="Fake GPU",
                created_at="2026-08-30T00:00:00+00:00",
                auto_sweep_enabled=True,
                sweep_address=DESTINATION,
            )
            result = run_local_once(
                profile,
                FoundPuzzle8Engine(),
                worker="local-test",
                lease_seconds=60,
                sweep_network=FakeNetwork(),
            )
            status = Coordinator(database).status()
            sweep_record = json.loads(
                (root / "sweep.json").read_text(encoding="utf-8")
            )
        self.assertTrue(result.found)
        self.assertEqual(result.sweep_state, "broadcast")
        self.assertIsNone(status["found_key_hex"])
        self.assertEqual(sweep_record["state"], "broadcast")
        self.assertEqual(sweep_record["destination_address"], DESTINATION)

    @staticmethod
    def _parse_single_input(signed: SignedSweep) -> dict[str, object]:
        parsed = SweepTests._parse_transaction(signed)
        if len(parsed["inputs"]) != 1:
            raise AssertionError("expected one transaction input")
        return {
            **parsed["inputs"][0],
            **{
                key: parsed[key]
                for key in ("output_value", "destination_script")
            },
        }

    @staticmethod
    def _parse_transaction(signed: SignedSweep) -> dict[str, object]:
        raw = bytes.fromhex(signed.raw_transaction_hex)
        offset = 4
        input_count = raw[offset]
        offset += 1
        inputs: list[dict[str, bytes]] = []
        for _ in range(input_count):
            offset += 32 + 4
            script_length = raw[offset]
            offset += 1
            script_sig = raw[offset : offset + script_length]
            offset += script_length + 4
            signature_length = script_sig[0]
            signature = script_sig[1 : 1 + signature_length]
            public_key_offset = 1 + signature_length
            public_key_length = script_sig[public_key_offset]
            public_key = script_sig[
                public_key_offset + 1 : public_key_offset + 1 + public_key_length
            ]
            inputs.append({"signature": signature, "public_key": public_key})
        if raw[offset] != 1:
            raise AssertionError("expected one transaction output")
        offset += 1
        output_value = int.from_bytes(raw[offset : offset + 8], "little")
        offset += 8
        output_script_length = raw[offset]
        offset += 1
        destination_script = raw[offset : offset + output_script_length]
        return {
            "inputs": inputs,
            "output_value": output_value,
            "destination_script": destination_script,
        }


if __name__ == "__main__":
    unittest.main()
