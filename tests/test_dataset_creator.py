import csv
import io
import random
import tempfile
from datetime import datetime, time, timedelta
from pathlib import Path
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from dataset_creator import (
    available_products_for_store,
    create_bank_card,
    create_transaction_receipt,
    MAX_CARD_USES,
    NUM_RECEIPTS,
    load_config,
    main,
)


class DatasetCreatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config()

    def test_every_store_has_products_with_configured_brands(self):
        for store in self.config["stores"]:
            with self.subTest(store=store["name"]):
                self.assertTrue(available_products_for_store(store, self.config))

    def test_transaction_category_and_brand_fit_selected_store(self):
        for _ in range(50):
            receipt = create_transaction_receipt(self.config)
            store = next(
                store
                for store in self.config["stores"]
                if store["name"] == receipt["store_name"]
            )

            self.assertIn(
                receipt["category"],
                available_products_for_store(store, self.config),
            )
            self.assertIn(
                receipt["brand"],
                [brand["name"] for brand in self.config["brands"][receipt["category"]]],
            )
            purchase_time = datetime.fromisoformat(receipt["timestamp"]).replace(tzinfo=None)
            latitude, longitude = receipt["coords"].split(",")
            branch = next(
                branch
                for branch in store["branches"]
                if f"{branch['lat']:.8f}" == latitude
                and f"{branch['lon']:.8f}" == longitude
            )
            self.assertEqual(purchase_time.year, 2026)
            opening = datetime.combine(purchase_time.date(), time.fromisoformat(branch["open"]))
            closing = datetime.combine(purchase_time.date(), time.fromisoformat(branch["close"]))
            if closing <= opening:
                closing += timedelta(days=1)
            self.assertGreaterEqual(purchase_time, opening)
            self.assertLess(purchase_time, closing)
            self.assertAlmostEqual(float(latitude), branch["lat"], places=8)
            self.assertAlmostEqual(float(longitude), branch["lon"], places=8)

    def test_main_writes_only_requested_csv_columns_and_keeps_debug_counters(self):
        columns = [
            "store_name",
            "timestamp",
            "coordinates",
            "category",
            "brand",
            "item_price",
            "card_number",
            "quantity",
            "receipt_number",
            "total_amount",
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "transactions.csv"
            output = io.StringIO()
            with patch("dataset_creator.OUTPUT_PATH", output_path), redirect_stdout(output):
                main()

            with output_path.open(encoding="utf-8-sig", newline="") as file:
                rows = list(csv.reader(file))

        self.assertEqual(rows[0], columns)
        self.assertEqual(len(rows), NUM_RECEIPTS + 1)
        self.assertEqual(len(rows[1]), len(columns))
        self.assertIn("Филиалов:", output.getvalue())

    def test_generated_card_uses_configured_bank_and_payment_system(self):
        card = create_bank_card(self.config)
        bank = next(
            bank for bank in self.config["banks"]
            if bank["name"] == card["bank"]
        )

        self.assertEqual(len(card["number"]), 16)
        self.assertTrue(card["number"].isdigit())
        self.assertIn(card["payment_system"], bank["payment_systems"])
        self.assertTrue(any(
            card["number"].startswith(prefix)
            for prefix in bank["payment_systems"][card["payment_system"]]["bins"]
        ))

    def test_card_usage_is_shared_across_receipts_and_stops_at_five(self):
        card = create_bank_card(self.config)

        for expected_uses in range(1, MAX_CARD_USES + 1):
            create_transaction_receipt(self.config, card)
            self.assertEqual(card["used"], expected_uses)

        with self.assertRaisesRegex(ValueError, "уже использована 5 раз"):
            create_transaction_receipt(self.config, card)
        self.assertEqual(card["used"], MAX_CARD_USES)

    def test_receipt_numbers_are_unique_within_a_branch(self):
        card = create_bank_card(self.config)
        used_receipt_numbers = {}
        receipt_numbers = iter((123, 123, 124))
        original_randint = random.randint

        def controlled_randint(start, end):
            if (start, end) == (1, 999999):
                return next(receipt_numbers)
            return original_randint(start, end)

        with (
            patch("dataset_creator.random.choice", side_effect=lambda values: values[0]),
            patch("dataset_creator.random.randint", side_effect=controlled_randint),
        ):
            first = create_transaction_receipt(
                self.config, card, used_receipt_numbers
            )
            second = create_transaction_receipt(
                self.config, card, used_receipt_numbers
            )

        self.assertEqual(first["store_name"], second["store_name"])
        self.assertEqual(first["coords"], second["coords"])
        self.assertEqual(first["receipt_number"], "№ 123")
        self.assertEqual(second["receipt_number"], "№ 124")
        self.assertEqual(len(used_receipt_numbers), 1)

    def test_main_never_uses_a_card_more_than_five_times(self):
        columns = [
            "store_name",
            "timestamp",
            "coordinates",
            "category",
            "brand",
            "item_price",
            "card_number",
            "quantity",
            "receipt_number",
            "total_amount",
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "transactions.csv"
            output = io.StringIO()
            with (
                patch("dataset_creator.OUTPUT_PATH", output_path),
                redirect_stdout(output),
            ):
                main()

            with output_path.open(encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file))

        self.assertEqual(len(rows), NUM_RECEIPTS)
        self.assertTrue(all(len(row) == len(columns) for row in rows))
        uses_by_card = {}
        for row in rows:
            card_number = row["card_number"]
            uses_by_card[card_number] = uses_by_card.get(card_number, 0) + 1
        self.assertTrue(all(uses <= MAX_CARD_USES for uses in uses_by_card.values()))
        if NUM_RECEIPTS == MAX_CARD_USES:
            self.assertEqual(list(uses_by_card.values()), [MAX_CARD_USES])


if __name__ == "__main__":
    unittest.main()
