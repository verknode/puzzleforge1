import unittest

from puzzleforge.crypto import (
    GENERATOR,
    base58_decode,
    decode_p2pkh,
    p2pkh_address_from_private_key,
    scalar_multiply,
)


class CryptoTests(unittest.TestCase):
    def test_private_key_one_matches_generator(self) -> None:
        self.assertEqual(scalar_multiply(1), GENERATOR)

    def test_known_compressed_p2pkh_vectors(self) -> None:
        vectors = {
            1: "1BgGZ9tcN4rm9KBzDn7KprQz87SZ26SAMH",
            3: "1CUNEBjYrCn2y1SdiUMohaKUi4wpP326Lb",
            7: "19ZewH8Kk1PDbSNdJ97FP4EiCjTRaZMZQA",
            8: "1EhqbyUMvvs7BfL8goY6qcPbD6YKfPqb7e",
        }
        for private_key, address in vectors.items():
            with self.subTest(private_key=private_key):
                self.assertEqual(p2pkh_address_from_private_key(private_key), address)

    def test_decode_registered_address(self) -> None:
        payload = decode_p2pkh("1PWo3JeB9jrGwfHDNpdGK54CRas7fsVzXU")
        self.assertEqual(len(payload), 20)

    def test_rejects_bad_checksum(self) -> None:
        with self.assertRaises(ValueError):
            decode_p2pkh("1PWo3JeB9jrGwfHDNpdGK54CRas7fsVzXV")

    def test_rejects_invalid_base58(self) -> None:
        with self.assertRaises(ValueError):
            base58_decode("0OIl")


if __name__ == "__main__":
    unittest.main()

