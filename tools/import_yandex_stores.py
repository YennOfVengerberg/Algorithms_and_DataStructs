"""Collect and verify Saint Petersburg store branches using Yandex Geosearch.

Set YANDEX_MAPS_API_KEY, then run:
    python tools/import_yandex_stores.py

The script writes an API-derived draft and a separate manual-review report.
It never treats the existing address or coordinates as verified, and it never
edits config/stores.json. Search results are candidates, not a guaranteed
exhaustive directory of every branch.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


PROJECT_DIR = Path(__file__).resolve().parents[1]
STORES_PATH = PROJECT_DIR / "config" / "stores.json"
PRODUCT_CATEGORIES_PATH = PROJECT_DIR / "config" / "categories.json"
STORE_CATEGORIES_PATH = PROJECT_DIR / "config" / "store_categories.json"
ADDITIONAL_SEARCHES_PATH = PROJECT_DIR / "config" / "additional_store_searches.json"
DRAFT_PATH = PROJECT_DIR / "config" / "stores_yandex_draft.json"
REVIEW_PATH = PROJECT_DIR / "config" / "stores_manual_review.json"
API_URL = "https://search-maps.yandex.ru/v1/"
PAGE_SIZE = 50
MAX_PAGES_PER_STORE = 20
REQUEST_DELAY_SECONDS = 0.1
MAX_REQUEST_ATTEMPTS = 3
REQUEST_TIMEOUT_SECONDS = 45
RETRY_BACKOFF_SECONDS = (2, 5)

# A coarse guard against results far outside Saint Petersburg. It is not a
# city-boundary polygon, so candidates still need address review.
CITY_BOUNDS = (29.4, 59.55, 31.1, 60.35)
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def normalize_name(value: str) -> str:
    return "".join(character for character in value.casefold().replace("ё", "е") if character.isalnum())


def _clock(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"(\d{2}):(\d{2})(?::(\d{2}))?", value)
    if not match:
        return None
    hour, minute, second = (int(part or 0) for part in match.groups())
    if hour > 23 or minute > 59 or second != 0:
        return None
    return f"{hour:02d}:{minute:02d}"


def reliable_daily_hours(hours: Any) -> tuple[str, str] | None:
    """Return one daily interval only when structured API data is unambiguous."""
    if not isinstance(hours, dict):
        return None
    availabilities = hours.get("Availabilities")
    if not isinstance(availabilities, list) or len(availabilities) != 1:
        return None

    availability = availabilities[0]
    if not isinstance(availability, dict) or availability.get("TwentyFourHours") is True:
        return None

    every_day = availability.get("Everyday") is True or all(
        availability.get(day) is True for day in WEEKDAYS
    )
    intervals = availability.get("Intervals")
    if not every_day or not isinstance(intervals, list) or len(intervals) != 1:
        return None

    interval = intervals[0]
    if not isinstance(interval, dict):
        return None
    opening = _clock(interval.get("from"))
    closing = _clock(interval.get("to"))
    if opening is None or closing is None or opening >= closing:
        return None
    return opening, closing


def parse_common_hours_text(value: Any) -> tuple[str, str] | None:
    """Extract a safe daily interval from a clear Yandex hours description."""
    if not isinstance(value, str) or not value.strip():
        return None

    text = value.casefold().replace("ё", "е").replace("–", "-").replace("—", "-")
    weekdays = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
    covered_days: set[int] = set()
    intervals: list[tuple[int, int]] = []

    for raw_segment in text.split(";"):
        segment = raw_segment.strip()
        if not segment or "перерыв" in segment:
            return None

        matches = list(re.finditer(r"\b\d{2}:\d{2}\s*-\s*\d{2}:\d{2}\b", segment))
        if "круглосуточно" in segment:
            if matches:
                return None
            opening, closing = 0, 23 * 60 + 59
        else:
            if len(matches) != 1:
                return None
            match = matches[0]
            start_text, end_text = re.split(r"\s*-\s*", match.group(), maxsplit=1)
            start, end = _clock(start_text), _clock(end_text)
            if start is None or end is None:
                return None
            opening = int(start[:2]) * 60 + int(start[3:])
            closing = int(end[:2]) * 60 + int(end[3:])
            if closing == 0:
                closing = 24 * 60
            if closing <= opening:
                return None

        day_spec = (
            segment[:matches[0].start()].strip(" ,")
            if matches
            else segment.replace("круглосуточно", "").strip(" ,")
        )
        if day_spec.startswith("ежедневно"):
            days = set(range(7))
            suffix = day_spec[len("ежедневно"):].strip(" ,")
            if suffix:
                return None
        else:
            days: set[int] = set()
            parts = [part.strip() for part in day_spec.split(",")]
            if not parts or any(not part for part in parts):
                return None
            valid_parts = True
            for part in parts:
                endpoints = part.split("-")
                if len(endpoints) == 1:
                    if endpoints[0] not in weekdays:
                        valid_parts = False
                        break
                    days.add(weekdays.index(endpoints[0]))
                elif len(endpoints) == 2 and all(endpoint in weekdays for endpoint in endpoints):
                    first, last = (weekdays.index(endpoint) for endpoint in endpoints)
                    if first > last:
                        valid_parts = False
                        break
                    days.update(range(first, last + 1))
                else:
                    valid_parts = False
                    break
            if not valid_parts or not days:
                return None

        if covered_days.intersection(days):
            return None
        covered_days.update(days)
        intervals.append((opening, closing))

    if covered_days != set(range(7)):
        return None

    common_open = max(opening for opening, _ in intervals)
    common_close = min(closing for _, closing in intervals)
    if common_close <= common_open:
        return None
    if common_close == 24 * 60:
        common_close -= 1
    return (
        f"{common_open // 60:02d}:{common_open % 60:02d}",
        f"{common_close // 60:02d}:{common_close % 60:02d}",
    )


def _in_city_bounds(lon: float, lat: float) -> bool:
    west, south, east, north = CITY_BOUNDS
    return west <= lon <= east and south <= lat <= north


def _distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius_m = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)
    haversine = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * earth_radius_m * math.asin(math.sqrt(haversine))


def _open_with_retries(request: Request) -> Any:
    for attempt in range(MAX_REQUEST_ATTEMPTS):
        try:
            return urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS)
        except HTTPError as error:
            retryable = error.code == 429 or 500 <= error.code <= 599
            status = error.code
            error.close()
            if not retryable or attempt == MAX_REQUEST_ATTEMPTS - 1:
                raise RuntimeError(f"Yandex API вернул HTTP {status}") from None
        except (URLError, TimeoutError) as error:
            if attempt == MAX_REQUEST_ATTEMPTS - 1:
                if isinstance(error, URLError):
                    reason = error.reason
                else:
                    reason = error
                raise RuntimeError(f"Ошибка соединения с Yandex API: {reason}") from None

        time.sleep(RETRY_BACKOFF_SECONDS[attempt])

    raise RuntimeError("Не удалось выполнить запрос к Yandex API")


def _candidate_from_feature(
    store_name: str,
    feature: Any,
    *,
    allow_name_mismatch: bool = False,
    allow_missing_hours: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    reasons: list[str] = []
    if not isinstance(feature, dict):
        return None, {"store": store_name, "reasons": ["Некорректная запись API"]}

    properties = feature.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    metadata = properties.get("CompanyMetaData")
    metadata = metadata if isinstance(metadata, dict) else {}

    result_name = properties.get("name") or metadata.get("name")
    result_name = result_name.strip() if isinstance(result_name, str) else ""
    if not result_name or (
        not allow_name_mismatch
        and normalize_name(result_name) != normalize_name(store_name)
    ):
        reasons.append("Название организации не совпало с названием сети")

    address_data = metadata.get("Address")
    address = ""
    if isinstance(address_data, dict):
        formatted = address_data.get("formatted")
        if isinstance(formatted, str):
            address = formatted.strip()
    if not address:
        value = metadata.get("address")
        if isinstance(value, str):
            address = value.strip()
    if not address:
        reasons.append("API не вернул адрес филиала")

    geometry = feature.get("geometry")
    coordinates = geometry.get("coordinates") if isinstance(geometry, dict) else None
    lon: float | None = None
    lat: float | None = None
    if isinstance(coordinates, list) and len(coordinates) >= 2:
        try:
            lon, lat = float(coordinates[0]), float(coordinates[1])
        except (TypeError, ValueError):
            lon, lat = None, None
    if lon is None or lat is None or not math.isfinite(lon) or not math.isfinite(lat):
        reasons.append("API не вернул корректные координаты")
    elif not _in_city_bounds(lon, lat):
        reasons.append("Координаты за пределами Санкт-Петербурга и ближайших окрестностей")

    description = properties.get("description")
    location_text = " ".join(
        text for text in (address, description if isinstance(description, str) else "") if text
    ).casefold()
    if "петербург" not in location_text and "санкт-петербург" not in location_text:
        reasons.append("Город не подтверждён текстом адреса API")

    hours = metadata.get("Hours")
    reliable_hours = reliable_daily_hours(hours)
    if reliable_hours is None and isinstance(hours, dict):
        reliable_hours = parse_common_hours_text(hours.get("text"))
    if reliable_hours is None:
        reasons.append("Нет однозначного одинакового расписания на каждый день")

    review_entry = {
        "store": store_name,
        "name": result_name or None,
        "address": address or None,
        "lat": round(lat, 8) if lat is not None and math.isfinite(lat) else None,
        "lon": round(lon, 8) if lon is not None and math.isfinite(lon) else None,
        "hours_text": hours.get("text") if isinstance(hours, dict) else None,
        "reasons": reasons,
    }

    if (
        not result_name
        or (
            not allow_name_mismatch
            and normalize_name(result_name) != normalize_name(store_name)
        )
        or not address
        or lon is None
        or lat is None
        or not math.isfinite(lon)
        or not math.isfinite(lat)
        or not _in_city_bounds(lon, lat)
    ):
        return None, review_entry
    if reliable_hours is None and not allow_missing_hours:
        review_entry["reasons"].append("Филиал не добавлен: нет надёжного расписания open-close")
        return None, review_entry

    branch: dict[str, Any] = {
        "address": address,
        "lat": round(lat, 8),
        "lon": round(lon, 8),
    }
    if reliable_hours is not None:
        branch["open"], branch["close"] = reliable_hours
    return branch, review_entry


def _fetch_page(api_key: str, query: str, skip: int) -> tuple[list[Any], int | None]:
    params = {
        "apikey": api_key,
        "text": query,
        "type": "biz",
        "lang": "ru_RU",
        "ll": "30.31413,59.93863",
        "spn": "1.7,0.8",
        "rspn": "1",
        "results": str(PAGE_SIZE),
        "skip": str(skip),
    }
    request = Request(
        f"{API_URL}?{urlencode(params)}",
        headers={"User-Agent": "SyntheticDatasetStoreImporter/1.0"},
    )
    try:
        with _open_with_retries(request) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError("Yandex API вернул некорректный JSON") from error

    if not isinstance(payload, dict):
        raise RuntimeError("Yandex API вернул неожиданный формат ответа")
    features = payload.get("features")
    if not isinstance(features, list):
        raise RuntimeError("В ответе Yandex API отсутствует список features")

    response_properties = payload.get("properties")
    response_properties = response_properties if isinstance(response_properties, dict) else {}
    response_metadata = response_properties.get("ResponseMetaData")
    response_metadata = response_metadata if isinstance(response_metadata, dict) else {}
    search_response = response_metadata.get("SearchResponse")
    search_response = search_response if isinstance(search_response, dict) else {}
    found = search_response.get("found")
    found = found if isinstance(found, int) and found >= 0 else None
    return features, found


def fetch_store_features(
    api_key: str,
    store_name: str,
    address_hint: str | None = None,
) -> tuple[list[Any], bool]:
    query = f"Санкт-Петербург, {store_name}"
    if address_hint:
        query = f"{query}, {address_hint}"
    features: list[Any] = []
    skip = 0
    truncated = False
    for page_number in range(MAX_PAGES_PER_STORE):
        time.sleep(REQUEST_DELAY_SECONDS)
        page, found = _fetch_page(api_key, query, skip)
        features.extend(page)
        skip += len(page)
        if not page or len(page) < PAGE_SIZE or (found is not None and skip >= found):
            break
        if page_number == MAX_PAGES_PER_STORE - 1:
            truncated = True
    return features, truncated


def load_store_list(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as file:
        stores = json.load(file)
    if not isinstance(stores, list):
        raise ValueError(f"{path} должен содержать JSON-массив магазинов")
    for store in stores:
        if not isinstance(store, dict) or not isinstance(store.get("name"), str):
            raise ValueError(f"Некорректная запись магазина в {path}")
        if not isinstance(store.get("theme"), str):
            raise ValueError(f"У магазина {store['name']} отсутствует theme")
    return stores


def load_store_categories(
    mapping_path: Path,
    product_categories_path: Path,
) -> dict[str, list[str]]:
    with mapping_path.open(encoding="utf-8") as file:
        mapping = json.load(file)
    with product_categories_path.open(encoding="utf-8") as file:
        product_categories = json.load(file)
    if not isinstance(mapping, dict):
        raise ValueError(f"{mapping_path} должен содержать объект магазин -> категории")
    if not isinstance(product_categories, dict):
        raise ValueError(f"{product_categories_path} должен содержать объект категорий")

    allowed_categories = set(product_categories)
    validated: dict[str, list[str]] = {}
    for store_name, categories in mapping.items():
        if not isinstance(store_name, str) or not isinstance(categories, list) or not categories:
            raise ValueError(f"Некорректный список категорий магазина {store_name!r}")
        if any(not isinstance(category, str) or category not in allowed_categories for category in categories):
            raise ValueError(f"У магазина {store_name} есть категории, которых нет в categories.json")
        if len(categories) != len(set(categories)):
            raise ValueError(f"У магазина {store_name} повторяются категории")
        validated[store_name] = categories
    return validated


def load_additional_searches(
    searches_path: Path,
    product_categories_path: Path,
) -> list[dict[str, Any]]:
    with searches_path.open(encoding="utf-8") as file:
        searches = json.load(file)
    with product_categories_path.open(encoding="utf-8") as file:
        product_categories = json.load(file)
    if not isinstance(searches, list) or not isinstance(product_categories, dict):
        raise ValueError(f"Некорректный формат {searches_path} или {product_categories_path}")

    validated: list[dict[str, Any]] = []
    for search in searches:
        if not isinstance(search, dict) or not isinstance(search.get("query"), str):
            raise ValueError(f"Некорректный поисковый запрос в {searches_path}")
        categories = search.get("theme")
        if (
            not isinstance(categories, list)
            or not categories
            or any(not isinstance(category, str) or category not in product_categories for category in categories)
            or len(categories) != len(set(categories))
        ):
            raise ValueError(f"Некорректные категории в поиске {search['query']!r}")
        max_stores = search.get("max_stores")
        max_branches = search.get("max_branches_per_store")
        if not isinstance(max_stores, int) or max_stores < 1:
            raise ValueError(f"Некорректный max_stores в поиске {search['query']!r}")
        if not isinstance(max_branches, int) or max_branches < 1:
            raise ValueError(f"Некорректный max_branches_per_store в поиске {search['query']!r}")
        validated.append({
            "query": search["query"],
            "theme": categories,
            "max_stores": max_stores,
            "max_branches_per_store": max_branches,
        })
    return validated


def collect(
    api_key: str,
    stores: list[dict[str, Any]],
    store_categories: dict[str, list[str]] | None = None,
    additional_searches: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    draft: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    existing_branch_checks: list[dict[str, Any]] = []

    for store in stores:
        store_name = store["name"]
        store_search_error: str | None = None
        try:
            features, truncated = fetch_store_features(api_key, store_name)
        except RuntimeError as error:
            features, truncated = [], False
            store_search_error = str(error)
            review.append({
                "store": store_name,
                "reasons": ["Не удалось выполнить поиск сети через API"],
                "api_error": store_search_error,
            })
        branches: list[dict[str, Any]] = []
        seen: set[str] = set()

        for feature in features:
            branch, review_entry = _candidate_from_feature(store_name, feature)
            metadata = feature.get("properties", {}).get("CompanyMetaData", {}) if isinstance(feature, dict) else {}
            organization_id = metadata.get("id") if isinstance(metadata, dict) else None
            if branch is not None:
                duplicate_key = str(organization_id) if organization_id else (
                    f"{branch['address']}|{branch['lat']}|{branch['lon']}"
                )
                if duplicate_key not in seen:
                    seen.add(duplicate_key)
                    branches.append(branch)
                if review_entry["reasons"]:
                    review.append(review_entry)
            else:
                review.append(review_entry)

        if not branches and store_search_error is None:
            review.append({
                "store": store_name,
                "reasons": ["Не удалось автоматически подтвердить ни одного филиала"],
            })
        if truncated:
            review.append({
                "store": store_name,
                "reasons": ["Достигнут предел страниц поиска; результаты могут быть неполными"],
            })

        for branch_number, existing in enumerate(store.get("branches", []), start=1):
            if not isinstance(existing, dict):
                existing_branch_checks.append({
                    "store": store_name,
                    "branch_number": branch_number,
                    "existing": existing,
                    "status": "manual_review",
                    "reason": "Некорректная запись филиала в исходном stores.json",
                })
                continue

            old_lat, old_lon = existing.get("lat"), existing.get("lon")
            check: dict[str, Any] = {
                "store": store_name,
                "branch_number": branch_number,
                "existing": {
                    "address": existing.get("address"),
                    "lat": old_lat,
                    "lon": old_lon,
                },
                "status": "manual_review",
            }

            if (
                not isinstance(old_lat, (int, float))
                or not isinstance(old_lon, (int, float))
                or not math.isfinite(old_lat)
                or not math.isfinite(old_lon)
            ):
                check["reason"] = "В существующей записи отсутствуют корректные координаты"
                existing_branch_checks.append(check)
                continue

            old_address = existing.get("address")
            if not isinstance(old_address, str) or not old_address.strip():
                check["reason"] = "В существующей записи отсутствует адрес"
                existing_branch_checks.append(check)
                continue

            try:
                checked_features, check_truncated = fetch_store_features(
                    api_key,
                    store_name,
                    old_address,
                )
            except RuntimeError as error:
                check["reason"] = "Не удалось перепроверить филиал через API"
                check["api_error"] = str(error)
                existing_branch_checks.append(check)
                continue
            nearby: list[tuple[float, dict[str, Any]]] = []
            for feature in checked_features:
                candidate, _ = _candidate_from_feature(
                    store_name,
                    feature,
                    allow_missing_hours=True,
                )
                if candidate is None:
                    continue
                distance = _distance_meters(
                    float(old_lat),
                    float(old_lon),
                    candidate["lat"],
                    candidate["lon"],
                )
                nearby.append((distance, candidate))

            if nearby:
                distance, candidate = min(nearby, key=lambda item: item[0])
                check["nearest_api_candidate"] = candidate
                check["nearest_candidate_distance_m"] = round(distance, 1)
                if distance <= 300:
                    check["reason"] = (
                        "Сравните адрес и координаты вручную: близкая точка API может "
                        "относиться к другому филиалу или помещению того же торгового центра"
                    )
                else:
                    check["reason"] = (
                        "Ближайший результат API дальше 300 м и не подтверждает, "
                        "что найден именно этот филиал"
                    )
            else:
                check["reason"] = (
                    "API не вернул однозначный филиал по текущему адресу; "
                    "исходные адрес и координаты не подтверждены"
                )
            if check_truncated:
                check["results_truncated"] = True
            existing_branch_checks.append(check)

        draft.append({
            "name": store_name,
            "theme": (store_categories or {}).get(store_name, [store["theme"]]),
            "branches": branches,
        })

    stores_by_name = {store["name"]: store for store in draft}
    for search in additional_searches or []:
        query = search["query"]
        try:
            features, truncated = fetch_store_features(api_key, query)
        except RuntimeError as error:
            review.append({
                "search_query": query,
                "reasons": ["Не удалось выполнить поиск новых магазинов через API"],
                "api_error": str(error),
            })
            continue

        discovered_names: set[str] = set()
        for feature in features:
            properties = feature.get("properties") if isinstance(feature, dict) else None
            properties = properties if isinstance(properties, dict) else {}
            metadata = properties.get("CompanyMetaData")
            metadata = metadata if isinstance(metadata, dict) else {}
            result_name = properties.get("name") or metadata.get("name")
            if not isinstance(result_name, str) or not result_name.strip():
                review.append({
                    "search_query": query,
                    "reasons": ["API вернул организацию без названия"],
                })
                continue
            result_name = result_name.strip()
            store_key = normalize_name(result_name)
            existing_store = stores_by_name.get(result_name)
            if existing_store is None and store_key not in {
                normalize_name(name) for name in stores_by_name
            } and len(discovered_names) >= search["max_stores"]:
                continue

            branch, review_entry = _candidate_from_feature(
                query,
                feature,
                allow_name_mismatch=True,
            )
            if branch is None:
                review_entry["search_query"] = query
                review.append(review_entry)
                continue
            if "open" not in branch or "close" not in branch:
                review_entry["search_query"] = query
                review_entry["reasons"].append(
                    "Филиал не добавлен: нет надёжного расписания open-close"
                )
                review.append(review_entry)
                continue

            if existing_store is None:
                matching_key = next(
                    (
                        name
                        for name in stores_by_name
                        if normalize_name(name) == store_key
                    ),
                    None,
                )
                existing_store = stores_by_name.get(matching_key) if matching_key else None
            if existing_store is None:
                existing_store = {
                    "name": result_name,
                    "theme": list(search["theme"]),
                    "branches": [],
                }
                stores_by_name[result_name] = existing_store
            else:
                existing_store["theme"] = list(dict.fromkeys(
                    existing_store["theme"] + search["theme"]
                ))

            identity = metadata.get("id")
            duplicate = any(
                (
                    (
                        identity
                        and str(identity) == str(old_branch.get("_organization_id"))
                    )
                    or (
                        old_branch["address"] == branch["address"]
                        and old_branch["lat"] == branch["lat"]
                        and old_branch["lon"] == branch["lon"]
                    )
                )
                for old_branch in existing_store["branches"]
            )
            if duplicate:
                continue
            if len(existing_store["branches"]) >= search["max_branches_per_store"]:
                continue

            if identity:
                branch["_organization_id"] = str(identity)
            existing_store["branches"].append(branch)
            discovered_names.add(result_name)

        if truncated:
            review.append({
                "search_query": query,
                "reasons": ["Результаты поиска ограничены максимальным числом страниц"],
            })

    for store in stores_by_name.values():
        for branch in store["branches"]:
            branch.pop("_organization_id", None)
    draft = list(stores_by_name.values())

    report = {
        "source": "Yandex Geosearch API",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "coverage_note": (
            "API ранжирует наиболее подходящие запросу организации и не гарантирует полный список. "
            "Каждый старый филиал проверяется отдельным запросом по текущему адресу, но совпадение "
            "кандидата API само по себе не подтверждает правильность адреса или координат. "
            "Ассортиментные категории — приближённое соответствие типа магазина, а не проверка "
            "наличия каждого товара в каждом филиале."
        ),
        "manual_review": review,
        "existing_branch_checks": existing_branch_checks,
    }
    return draft, report


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    api_key = os.environ.get("YANDEX_MAPS_API_KEY")
    if not api_key:
        print(
            "Не задан YANDEX_MAPS_API_KEY. Установите ключ в переменной окружения; "
            "не добавляйте его в исходный код.",
            file=sys.stderr,
        )
        return 2

    try:
        stores = load_store_list(STORES_PATH)
        store_categories = load_store_categories(STORE_CATEGORIES_PATH, PRODUCT_CATEGORIES_PATH)
        additional_searches = load_additional_searches(
            ADDITIONAL_SEARCHES_PATH,
            PRODUCT_CATEGORIES_PATH,
        )
        draft, report = collect(api_key, stores, store_categories, additional_searches)
        _write_json(DRAFT_PATH, draft)
        _write_json(REVIEW_PATH, report)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(f"Импорт не выполнен: {error}", file=sys.stderr)
        return 1

    branch_count = sum(len(store["branches"]) for store in draft)
    print(f"Черновик: {DRAFT_PATH} ({len(draft)} сетей, {branch_count} филиалов)")
    print(f"Ручная проверка: {REVIEW_PATH} ({len(report['manual_review'])} записей)")
    print(
        "Проверки старых филиалов: "
        f"{len(report['existing_branch_checks'])} (все требуют просмотра человеком)"
    )
    api_errors = sum("api_error" in item for item in report["manual_review"])
    api_errors += sum("api_error" in item for item in report["existing_branch_checks"])
    if api_errors:
        print(
            f"Внимание: {api_errors} запросов API не выполнены даже после повторов; "
            "проверьте их в stores_manual_review.json."
        )
    print("config/stores.json не изменён.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
