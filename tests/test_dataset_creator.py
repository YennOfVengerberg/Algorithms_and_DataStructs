import unittest

from dataset_creator import (
    available_products_for_store,
    create_bank_card,
    create_transaction_receipt,
    load_config,
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
            store = receipt["store"]

            self.assertIn(
                receipt["category"],
                available_products_for_store(store, self.config),
            )
            self.assertIn(receipt["brand"], self.config["brands"][receipt["category"]])

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


if __name__ == "__main__":
    unittest.main()
