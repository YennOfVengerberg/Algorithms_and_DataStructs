import json
import yaml
from pathlib import Path

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
      #  "settings": settings,
    }

# Использование:
config = load_config()
print(f"Магазинов: {len(config['stores'])}")
print(f"Категорий: {sum(len(v) for v in config['categories'].values())}")
print(f"Брендов: {sum(len(v) for v in config['brands'].values())}")