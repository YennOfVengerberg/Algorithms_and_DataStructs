import unittest
from urllib.error import HTTPError
from unittest.mock import patch
from urllib.request import Request

from tools.import_yandex_stores import (
    _open_with_retries,
    _candidate_from_feature,
    collect,
    normalize_name,
    parse_common_hours_text,
    reliable_daily_hours,
)


class YandexStoreImportTests(unittest.TestCase):
    @patch("tools.import_yandex_stores.time.sleep")
    @patch("tools.import_yandex_stores.urlopen")
    def test_retries_gateway_timeout_then_succeeds(self, urlopen, sleep):
        response = object()
        urlopen.side_effect = [HTTPError("https://example.test", 504, "Gateway Timeout", {}, None), response]

        result = _open_with_retries(Request("https://example.test"))

        self.assertIs(result, response)
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(2)

    @patch("tools.import_yandex_stores.time.sleep")
    @patch("tools.import_yandex_stores.urlopen")
    def test_does_not_retry_nontransient_http_error(self, urlopen, sleep):
        urlopen.side_effect = HTTPError("https://example.test", 400, "Bad Request", {}, None)

        with self.assertRaisesRegex(RuntimeError, "HTTP 400"):
            _open_with_retries(Request("https://example.test"))

        urlopen.assert_called_once()
        sleep.assert_not_called()

    def test_name_normalization_handles_punctuation_and_yo(self):
        self.assertEqual(normalize_name("М.Видео"), normalize_name("М-видео"))
        self.assertEqual(normalize_name("Ёлка"), normalize_name("елка"))

    def test_extracts_only_one_structured_daily_interval(self):
        self.assertEqual(
            reliable_daily_hours({
                "Availabilities": [{
                    "Everyday": True,
                    "Intervals": [{"from": "10:00:00", "to": "22:00:00"}],
                }]
            }),
            ("10:00", "22:00"),
        )

    def test_rejects_different_or_ambiguous_hours(self):
        self.assertIsNone(reliable_daily_hours({
            "Availabilities": [{
                "Monday": True,
                "Intervals": [{"from": "10:00", "to": "22:00"}],
            }]
        }))
        self.assertIsNone(reliable_daily_hours({
            "Availabilities": [{
                "Everyday": True,
                "Intervals": [
                    {"from": "10:00", "to": "14:00"},
                    {"from": "15:00", "to": "22:00"},
                ],
            }]
        }))

    def test_parses_common_interval_for_weekly_hours_without_day_splitting(self):
        self.assertEqual(
            parse_common_hours_text("пн-пт 08:30–22:00; сб,вс 09:00–22:00"),
            ("09:00", "22:00"),
        )

    def test_parses_round_the_clock_hours_at_minute_precision(self):
        self.assertEqual(
            parse_common_hours_text("ежедневно, круглосуточно"),
            ("00:00", "23:59"),
        )

    def test_rejects_weekly_hours_with_breaks_or_incomplete_day_coverage(self):
        self.assertIsNone(
            parse_common_hours_text("ежедневно, 08:00–22:00, перерыв 14:00–14:30")
        )
        self.assertIsNone(parse_common_hours_text("пн-пт 09:00–18:00"))

    def test_branch_uses_lon_lat_order_and_rounds_coordinates(self):
        feature = {
            "properties": {
                "name": "М.Видео",
                "description": "Санкт-Петербург",
                "CompanyMetaData": {
                    "id": "123",
                    "address": "Лиговский проспект, 30",
                    "Hours": {
                        "Availabilities": [{
                            "Everyday": True,
                            "Intervals": [{"from": "10:00", "to": "22:00"}],
                        }],
                        "text": "ежедневно, 10:00–22:00",
                    },
                },
            },
            "geometry": {
                "type": "Point",
                "coordinates": [30.360538123, 59.927417876],
            },
        }

        branch, review = _candidate_from_feature("М.Видео", feature)

        self.assertEqual(branch, {
            "address": "Лиговский проспект, 30",
            "lat": 59.92741788,
            "lon": 30.36053812,
            "open": "10:00",
            "close": "22:00",
        })
        self.assertEqual(review["reasons"], [])

    def test_clear_daily_hours_text_is_used_when_structured_hours_are_missing(self):
        feature = {
            "properties": {
                "name": "М.Видео",
                "description": "Санкт-Петербург",
                "CompanyMetaData": {
                    "address": "Лиговский проспект, 30",
                    "Hours": {"text": "ежедневно 10:00–22:00"},
                },
            },
            "geometry": {"type": "Point", "coordinates": [30.36, 59.93]},
        }

        branch, review = _candidate_from_feature("М.Видео", feature)

        self.assertEqual((branch["open"], branch["close"]), ("10:00", "22:00"))
        self.assertEqual(review["reasons"], [])

    def test_uncertain_hours_are_rejected_and_flagged(self):
        feature = {
            "properties": {
                "name": "М.Видео",
                "description": "Санкт-Петербург",
                "CompanyMetaData": {
                    "address": "Лиговский проспект, 30",
                    "Hours": {"text": "пн-пт 10:00–22:00"},
                },
            },
            "geometry": {"type": "Point", "coordinates": [30.36, 59.93]},
        }

        branch, review = _candidate_from_feature("М.Видео", feature)

        self.assertIsNone(branch)
        self.assertIn(
            "Нет однозначного одинакового расписания на каждый день",
            review["reasons"],
        )
        self.assertIn(
            "Филиал не добавлен: нет надёжного расписания open-close",
            review["reasons"],
        )

    @patch("tools.import_yandex_stores.fetch_store_features")
    def test_existing_branch_is_reported_for_manual_address_and_coordinate_check(self, fetch):
        feature = {
            "properties": {
                "name": "М.Видео",
                "description": "Санкт-Петербург",
                "CompanyMetaData": {
                    "id": "123",
                    "address": "Санкт-Петербург, Лиговский проспект, 30",
                    "Hours": {
                        "Availabilities": [{
                            "Everyday": True,
                            "Intervals": [{"from": "10:00", "to": "22:00"}],
                        }],
                    },
                },
            },
            "geometry": {"type": "Point", "coordinates": [30.3605, 59.9274]},
        }
        fetch.side_effect = [([feature], False), ([feature], False)]
        stores = [{
            "name": "М.Видео",
            "theme": "электроника",
            "branches": [{
                "address": "Ошибочный адрес",
                "lat": 59.9,
                "lon": 30.3,
                "open": "10:00",
                "close": "22:00",
            }],
        }]

        draft, report = collect("test-key", stores)

        self.assertEqual(draft[0]["branches"][0]["address"], "Санкт-Петербург, Лиговский проспект, 30")
        self.assertEqual(draft[0]["theme"], ["электроника"])
        check = report["existing_branch_checks"][0]
        self.assertEqual(check["existing"]["address"], "Ошибочный адрес")
        self.assertEqual(check["status"], "manual_review")
        self.assertGreater(check["nearest_candidate_distance_m"], 300)
        self.assertIn("не подтверждает", check["reason"])
        self.assertEqual(fetch.call_count, 2)

    @patch("tools.import_yandex_stores.fetch_store_features")
    def test_api_failure_is_recorded_and_import_continues(self, fetch):
        fetch.side_effect = [
            RuntimeError("Yandex API вернул HTTP 504"),
            RuntimeError("Yandex API вернул HTTP 504"),
        ]
        stores = [{
            "name": "М.Видео",
            "theme": "электроника",
            "branches": [{
                "address": "Лиговский проспект, 30",
                "lat": 59.9,
                "lon": 30.3,
            }],
        }]

        draft, report = collect("test-key", stores)

        self.assertEqual(draft, [{
            "name": "М.Видео",
            "theme": ["электроника"],
            "branches": [],
        }])
        self.assertEqual(report["manual_review"][0]["api_error"], "Yandex API вернул HTTP 504")
        self.assertEqual(
            report["existing_branch_checks"][0]["api_error"],
            "Yandex API вернул HTTP 504",
        )

    def test_store_category_mapping_contains_only_configured_categories(self):
        import json
        from tools.import_yandex_stores import (
            PRODUCT_CATEGORIES_PATH,
            STORE_CATEGORIES_PATH,
            ADDITIONAL_SEARCHES_PATH,
            load_additional_searches,
            load_store_categories,
        )

        mapping = load_store_categories(STORE_CATEGORIES_PATH, PRODUCT_CATEGORIES_PATH)
        self.assertIn("зоотовары", mapping["Пятёрочка"])
        self.assertIn("товары для кошек", mapping["Пятёрочка"])
        with PRODUCT_CATEGORIES_PATH.open(encoding="utf-8") as file:
            categories = set(json.load(file))
        self.assertTrue(all(category in categories for values in mapping.values() for category in values))
        additional = load_additional_searches(ADDITIONAL_SEARCHES_PATH, PRODUCT_CATEGORIES_PATH)
        covered = {category for values in mapping.values() for category in values}
        covered.update(category for search in additional for category in search["theme"])
        self.assertEqual(covered, categories)

    @patch("tools.import_yandex_stores.fetch_store_features")
    def test_additional_searches_add_limited_shops_with_category_lists(self, fetch):
            def make_feature(name, number):
                return {
                    "properties": {
                        "name": name,
                        "description": "Санкт-Петербург",
                        "CompanyMetaData": {
                            "id": str(number),
                            "address": f"Санкт-Петербург, тестовая улица, {number}",
                            "Hours": {
                                "Availabilities": [{
                                    "Everyday": True,
                                    "Intervals": [{"from": "09:00", "to": "21:00"}],
                                }],
                            },
                        },
                    },
                    "geometry": {
                        "type": "Point",
                        "coordinates": [30.3 + number / 1000, 59.9 + number / 1000],
                    },
                }

            fetch.return_value = ([
                make_feature("Авто-магазин 1", 1),
                make_feature("Авто-магазин 1", 2),
                make_feature("Авто-магазин 1", 3),
                make_feature("Авто-магазин 1", 4),
                make_feature("Авто-магазин 2", 5),
                make_feature("Авто-магазин 3", 6),
                make_feature("Авто-магазин 4", 7),
            ], False)
            searches = [{
                "query": "магазин автозапчастей",
                "theme": ["автотовары", "автохимия", "автозапчасти"],
                "max_stores": 3,
                "max_branches_per_store": 3,
            }]

            draft, _ = collect("test-key", [], additional_searches=searches)

            self.assertEqual(len(draft), 3)
            first_store = next(store for store in draft if store["name"] == "Авто-магазин 1")
            self.assertEqual(first_store["theme"], ["автотовары", "автохимия", "автозапчасти"])
            self.assertEqual(len(first_store["branches"]), 3)
            self.assertNotIn("_organization_id", first_store["branches"][0])


if __name__ == "__main__":
    unittest.main()
