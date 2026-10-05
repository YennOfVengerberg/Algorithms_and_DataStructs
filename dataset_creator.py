import json
import math
from pathlib import Path

import random
from datetime import datetime, date, time, timedelta

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font

CONFIG_DIR = Path("config")
OUTPUT_PATH = Path("output") / "transactions.xlsx"
MAX_CARD_USES = 5
NUM_RECEIPTS = int(input("Введите количество чеков для генерации: "))


def load_config() -> dict:
    """Загрузка всех конфигов в один словарь."""

    with open(CONFIG_DIR / "stores.json", "r", encoding="utf-8") as f:
        stores = json.load(f)

    with open(CONFIG_DIR / "categories.json", "r", encoding="utf-8") as f:
        categories = json.load(f)

    with open(CONFIG_DIR / "brands.json", "r", encoding="utf-8") as f:
        brands = json.load(f)

    with open(CONFIG_DIR / "banks.json", "r", encoding="utf-8") as f:
        banks = json.load(f)

    with open(CONFIG_DIR / "store_price_model.json", "r", encoding="utf-8") as f:
        store_price_model = json.load(f)

    #with open(CONFIG_DIR / "settings.yaml", "r", encoding="utf-8") as f:
     #   settings = yaml.safe_load(f)

    return {
        "stores": stores,
        "categories": categories,
        "brands": brands,
        "banks": banks,
        "store_price_model": store_price_model,
    }


def store_price_multiplier(store: dict, config: dict) -> float:
    """Return the configured synthetic price multiplier for a store."""
    store_name = store.get("name")
    try:
        tier = config["store_price_model"]["store_tiers"][store_name]
        multiplier = config["store_price_model"]["tier_multipliers"][tier]
    except KeyError as error:
        raise ValueError(
            f"Для магазина {store_name!r} не настроен ценовой коэффициент."
        ) from error

    if (
        isinstance(multiplier, bool)
        or not isinstance(multiplier, (int, float))
        or not math.isfinite(multiplier)
        or multiplier <= 0
    ):
        raise ValueError(
            f"Некорректный ценовой коэффициент для магазина {store_name!r}: "
            f"{multiplier!r}"
        )
    return float(multiplier)


def available_products_for_store(store: dict, config: dict) -> list[str]:
    """Вернуть товары магазина, для которых заданы бренды."""
    themes = store.get("theme", [])
    if isinstance(themes, str):
        themes = [themes]

    products = dict.fromkeys(
        product
        for theme in themes
        for product in config["categories"].get(theme, [])
        if config["brands"].get(product)
    )
    if not products:
        raise ValueError(
            f"Для магазина {store.get('name', '<без названия>')} "
            "не найдено товаров с такими брендами."
        )
    return list(products)


def create_bank_card(config: dict) -> dict:
    """Создание новой карты по конфигурации банка."""
    banks = config["banks"]
    bank = random.choices(
        banks,
        weights=[item["weight"] for item in banks],
        k=1,
    )[0]

    payment_systems = bank["payment_systems"]
    payment_system = random.choices(
        list(payment_systems),
        weights=[item["weight"] for item in payment_systems.values()],
        k=1,
    )[0]
    bin_number = random.choice(payment_systems[payment_system]["bins"])
    if not bin_number.isdigit() or len(bin_number) >= 16:
        raise ValueError(f"Некорректный префикс карты: {bin_number!r}")

    card_number = bin_number + "".join(
        str(random.randint(0, 9)) for _ in range(16 - len(bin_number))
    )

    return {
        "number": card_number,
        "bank": bank["name"],
        "payment_system": payment_system,
        "used": 0,
    }


def check_card_exceeded_limit(card: dict) -> bool:
    """Проверка, превышен ли лимит использования карты."""
    return card["used"] >= MAX_CARD_USES


def create_transaction_receipt(
    config: dict,
    card: dict | None = None,
    used_receipt_numbers: dict[tuple[str, str, str, str], set[int]] | None = None,
) -> dict:
    """Создание одной записи чека с несколькими товарными позициями.

    Товары возвращаются списком словарей с категорией, брендом, ценой
    и количеством. Общее количество единиц товара в чеке — от 2 до 50.
    """

    # Выбираем случайный магазин и филиал
    store = random.choice(config["stores"])
    if not store.get("branches"):
        raise ValueError(f"У магазина {store.get('name')} нет филиалов.")
    branch = random.choice(store["branches"])  # ожидается, что у ветки есть 'open' и 'close'

    available_products = available_products_for_store(store, config)
    price_multiplier = store_price_multiplier(store, config)
    total_quantity = random.randint(2, 50)
    item_count = random.randint(1, min(5, len(available_products), total_quantity)) # количество товаров минимум 2 - категорий или единиц?
    selected_products = [
        (product, random.choice(config["brands"][product]))
        for product in random.sample(available_products, item_count)
    ]
    quantities = [1] * item_count
    for _ in range(total_quantity - item_count):
        quantities[random.randrange(item_count)] += 1

    products = []
    for (product, brand_entry), quantity in zip(selected_products, quantities):
        brand_name = (
            brand_entry.get("name")
            if isinstance(brand_entry, dict)
            else str(brand_entry)
        )
        min_p = None
        max_p = None
        if isinstance(brand_entry, dict):
            min_p = (
                brand_entry.get("min_price")
                or brand_entry.get("price_min")
                or brand_entry.get("min")
            )
            max_p = (
                brand_entry.get("max_price")
                or brand_entry.get("price_max")
                or brand_entry.get("max")
            )
        try:
            min_price = int(min_p) if min_p is not None else 1000
        except (TypeError, ValueError):
            min_price = 1000
        try:
            max_price = (
                int(max_p) if max_p is not None else max(min_price, 10000)
            )
        except (TypeError, ValueError):
            max_price = max(min_price, 10000)
        if max_price < min_price:
            max_price = min_price
        min_price = max(1, round(min_price * price_multiplier))
        max_price = max(min_price, round(max_price * price_multiplier))
        products.append({
            "category": product,
            "brand": brand_name,
            "item_price": random.randint(min_price, max_price),
            "quantity": quantity,
        })

    # Если карту не передали, создаём отдельную (удобно для одиночного вызова).
    if card is None:
        card = create_bank_card(config)
    if check_card_exceeded_limit(card):
        raise ValueError("Эта банковская карта уже использована 5 раз.")

    # Форматируем номер в группы по 4 цифры.
    card_number = card["number"]
    if len(card_number) >= 16:
        card_number_fmt = " ".join(card_number[i:i+4] for i in range(0, 16, 4))
    else:
        card_number_fmt = card_number

    if used_receipt_numbers is None:
        used_receipt_numbers = {}
    latitude = branch.get("lat", branch.get("latitude", branch.get("y")))
    longitude = branch.get("lon", branch.get("longitude", branch.get("x")))
    branch_key = (
        str(store.get("name", "")),
        str(branch.get("address", "")),
        str(latitude),
        str(longitude),
    )
    branch_receipt_numbers = used_receipt_numbers.setdefault(branch_key, set())
    if len(branch_receipt_numbers) >= 999999:
        raise RuntimeError(
            f"Для филиала {branch.get('address')} закончились уникальные номера чеков."
        )

    receipt_number_value = random.randint(1, 999999)
    while receipt_number_value in branch_receipt_numbers:
        receipt_number_value = random.randint(1, 999999)
    receipt_number = f"№ {receipt_number_value}"

    # Случайная дата 2026 года и время в общем интервале работы филиала.
    open_s = branch.get("open")
    close_s = branch.get("close")
    if not open_s or not close_s:
        raise ValueError(f"У филиала {branch.get('address')} не заданы часы работы.")
    try:
        opening = time.fromisoformat(open_s)
        closing = time.fromisoformat(close_s)
    except ValueError as error:
        raise ValueError(
            f"Некорректные часы работы филиала {branch.get('address')}: "
            f"{open_s!r}–{close_s!r}"
        ) from error

    first_day = date(2026, 1, 1)
    day_count = (date(2026, 12, 31) - first_day).days
    purchase_date = first_day + timedelta(days=random.randrange(day_count))
    start = datetime.combine(purchase_date, opening)
    end = datetime.combine(purchase_date, closing)
    if end < start:
        end += timedelta(days=1)
    total_minutes = int((end - start).total_seconds() // 60)
    last_open_minute = max(0, total_minutes - 1)
    chosen_dt = start + timedelta(minutes=random.randint(0, last_open_minute))
    timestamp = chosen_dt.isoformat(timespec="minutes")+"+03:00"

    # Координаты (объединяем широту и долготу через запятую)
    lat = branch.get("lat") or branch.get("latitude") or branch.get("y")
    lon = branch.get("lon") or branch.get("longitude") or branch.get("x")
    coords = ""
    try:
        if lat is not None and lon is not None:
            coords = f"{float(lat):.8f},{float(lon):.8f}"
    except Exception:
        coords = f"{lat},{lon}"

    total_amount = sum(
        item["item_price"] * item["quantity"] for item in products
    )

    receipt = {
        "store_name": store.get("name"),
        "timestamp": timestamp,
        "coords": coords,
        "products": products,
        "card_number": card_number_fmt,
        "receipt_number": receipt_number,
        "total_amount": total_amount,
    }
    branch_receipt_numbers.add(receipt_number_value)
    card["used"] += 1
    return receipt


def main() -> None:
    config = load_config()

    # Эти счётчики оставлены для отладки.
    # print(f"Магазинов: {len(config['stores'])}")
    # print(f"Филиалов: {sum(len(store['branches']) for store in config['stores'])}")
    # print(f"Банков: {len(config['banks'])}")
    # print(f"Категорий товаров: {sum(len(v) for v in config['categories'].values())}")
    # print(f"Брендов товаров: {sum(len(v) for v in config['brands'].values())}")

    headers = [
        "Название магазина",
        "Дата и время",
        "Координаты",
        "Категория",
        "Бренд",
        "Цена товара, руб.",
        "Количество, шт.",
        "Номер карты",
        "Номер чека",
        "Итого по чеку, руб.",
    ]
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Чеки"
    worksheet.append(headers)
    for cell in worksheet[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    column_widths = [24, 24, 24, 28, 22, 20, 18, 24, 16, 22]
    for column_index, width in enumerate(column_widths, start=1):
        worksheet.column_dimensions[worksheet.cell(1, column_index).column_letter].width = width

    num_cards = (NUM_RECEIPTS + MAX_CARD_USES - 1) // MAX_CARD_USES
    cards = []
    card_numbers = set()
    while len(cards) < num_cards:
        card = create_bank_card(config)
        if card["number"] not in card_numbers:
            cards.append(card)
            card_numbers.add(card["number"])

    used_receipt_numbers: dict[tuple[str, str, str, str], set[int]] = {}
    for _ in range(NUM_RECEIPTS):
        available_cards = [
            card for card in cards if not check_card_exceeded_limit(card)
        ]
        if not available_cards:
            raise RuntimeError(
                "Закончились банковские карты до создания всех чеков."
            )
        card = random.choice(available_cards)
        receipt = create_transaction_receipt(
            config, card, used_receipt_numbers
        )
        for product_index, product in enumerate(receipt["products"]):
            worksheet.append([
                receipt["store_name"] if product_index == 0 else None,
                receipt["timestamp"] if product_index == 0 else None,
                receipt["coords"] if product_index == 0 else None,
                product["category"],
                product["brand"],
                product["item_price"],
                product["quantity"],
                receipt["card_number"] if product_index == 0 else None,
                receipt["receipt_number"] if product_index == 0 else None,
                receipt["total_amount"] if product_index == 0 else None,
            ])

    workbook.save(OUTPUT_PATH)

    print(f"Данные сохранены в {OUTPUT_PATH}")


if __name__ == "__main__":
    main()