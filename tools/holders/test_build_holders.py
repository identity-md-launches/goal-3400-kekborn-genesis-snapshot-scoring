"""Run with python3 -B tools/holders/test_build_holders.py."""

from contextlib import redirect_stderr, redirect_stdout
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from build_holders import main


FIRST = "0x" + "a" * 40
SECOND = "0x" + "b" * 40
THIRD = "0x" + "c" * 40
ZERO = "0x" + "d" * 40


class HolderFixtureTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.output = self.root / "holders.csv"
        self.eth = [[SECOND, "1", True], [FIRST, "1000000000000000001", False]]
        self.write_chain("ethereum", self.eth)
        self.write_chain("robinhood", [[ZERO, "0", False], [THIRD, "5", False], [FIRST, "2", None]])

    def write_chain(self, chain, rows, **overrides):
        path = self.root / "sources" / f"chain-{chain}"
        path.mkdir(parents=True, exist_ok=True)
        (path / "holders.json").write_text(json.dumps({
            "columns": ["wallet", "balanceRaw", "isContract"], "rows": rows,
        }), encoding="utf-8")
        payload = "".join(f"{wallet}:{raw}\n" for wallet, raw, _ in sorted(rows))
        scan = {
            "supplyRaw": str(sum(int(row[1]) for row in rows)),
            "holdersDigest": hashlib.sha256(payload.encode("ascii")).hexdigest(),
        }
        scan.update(overrides)
        (path / "scan.json").write_text(json.dumps(scan), encoding="utf-8")

    def run_builder(self, arguments, status, message):
        log = io.StringIO()
        with redirect_stdout(log), redirect_stderr(log):
            self.assertEqual(main(self.root, arguments), status)
        self.assertIn(message, log.getvalue())

    def test_merge_and_check(self):
        self.run_builder([], 0, "Both chains: 1; CSV data rows: 3")
        with self.output.open(newline="", encoding="ascii") as stream:
            header, *rows = csv.reader(stream)
        self.assertEqual(header, [
            "wallet", "eth_balance_raw", "rh_balance_raw", "total_balance_raw",
            "eth_balance", "rh_balance", "total_balance", "on_ethereum", "on_robinhood",
            "is_contract_eth", "is_contract_rh",
        ])
        self.assertEqual(rows, [
            [FIRST, "1000000000000000001", "2", "1000000000000000003",
             "1.000000000000000001", "0.000000000000000002", "1.000000000000000003",
             "true", "true", "false", ""],
            [SECOND, "1", "0", "1", "0.000000000000000001", "0.000000000000000000",
             "0.000000000000000001", "true", "false", "true", ""],
            [THIRD, "0", "5", "5", "0.000000000000000000", "0.000000000000000005",
             "0.000000000000000005", "false", "true", "", "false"],
        ])
        self.run_builder(["--check"], 0, "matches the offline rebuild")
        self.output.write_bytes(b"changed\n")
        self.run_builder(["--check"], 1, "differs from the offline rebuild")
        self.assertEqual(self.output.read_bytes(), b"changed\n")

    def test_source_validation(self):
        cases = [
            (self.eth + [["0x" + FIRST[2:].upper(), "1", False]], {}, "duplicate wallet"),
            ([[FIRST, "-1", False]], {}, "negative balance"),
            (self.eth, {"supplyRaw": "0"}, "differs from supply"),
            (self.eth, {"holdersDigest": "invalid"}, "holdersDigest mismatch"),
        ]
        for rows, overrides, message in cases:
            with self.subTest(message=message):
                self.write_chain("ethereum", rows, **overrides)
                self.run_builder([], 1, message)
                self.assertFalse(self.output.exists())

    def test_missing_source(self):
        (self.root / "sources/chain-robinhood/scan.json").unlink()
        self.run_builder([], 1, "scan.json")
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
