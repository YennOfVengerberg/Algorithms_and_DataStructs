import json
from pathlib import Path

import random

CONFIG_DIR = Path("config")


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

    #with open(CONFIG_DIR / "settings.yaml", "r", encoding="utf-8") as f:
     #   settings = yaml.safe_load(f)

    return {
        "stores": stores,
        "categories": categories,
        "brands": brands,
        "banks": banks,
    }


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
    return card["used"] >= 5

def create_transaction_receipt(config: dict) -> dict:
    """Создание случайной транзакции на основе конфигурации."""

    # Выбираем случайный магазин
    store = random.choice(config["stores"])
    if not store["branches"]:
        raise ValueError(f"У магазина {store['name']} нет филиалов.")
    branch = random.choice(store["branches"])

    # Сначала выбираем подходящий этому магазину товар, затем его бренд.
    category = random.choice(available_products_for_store(store, config))
    brand = random.choice(config["brands"][category])

    # Создаем банковскую карту
    card = create_bank_card(config)

    # Проверяем, превышен ли лимит использования карты
    if check_card_exceeded_limit(card):
        return None  # Если лимит превышен, возвращаем None

    # Увеличиваем счетчик использования карты
    card["used"] += 1

    return {
        "store": store,
        "branch": branch,
        "category": category,
        "brand": brand,
        "card": card,
        "receipt_id": random.randint(1, 999999),
        "timestamp": 2
    }


def main() -> None:
    config = load_config()
    print(f"Магазинов: {len(config['stores'])}")
    print(f"Филиалов: {sum(len(store['branches']) for store in config['stores'])}")
    print(f"Банков: {len(config['banks'])}")
    print(f"Категорий товаров: {sum(len(v) for v in config['categories'].values())}")
    print(f"Брендов товаров: {sum(len(v) for v in config['brands'].values())}")

    for _ in range(1):
        receipt = create_transaction_receipt(config)
        print(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()