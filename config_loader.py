import json
from pathlib import Path

def load_catalog(path: str = "config.json") -> tuple[list[str], list[str]]:
    p = Path(path)
    if not p.exists():
        # дефолты на случай отсутствия файла
        return (["Москва", "Тверь", "Санкт-Петербург", "Зеленоград"],
                ["Вентиляция", "Кондиционирование", "Электрика", "Сантехника"])
    data = json.loads(p.read_text(encoding="utf-8"))
    cities = data.get("cities", []) or []
    categories = data.get("categories", []) or []
    return cities, categories
