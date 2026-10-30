"""Создание Windows ICO и синхронизация иконок приложения из исходного PNG."""

import argparse
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent
ICON_SIZES = [(size, size) for size in (16, 20, 24, 32, 40, 48, 64, 128, 256)]


def create_icon(source_path=None):
    source = Path(source_path) if source_path else ROOT / "icon.png"
    with Image.open(source) as original:
        if original.format != "PNG" or original.width != original.height:
            raise ValueError("Для иконки нужен квадратный PNG")
        image = original.convert("RGBA")
    png = source.read_bytes()
    image.save(ROOT / "icon.ico", format="ICO", sizes=ICON_SIZES)
    # Сохраняем исходные пиксели и фон присланной картинки без изменений.
    (ROOT / "icon.png").write_bytes(png)
    (ROOT / "design" / "icon.png").write_bytes(png)
    print("Updated icon.png, icon.ico and design/icon.png")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path, help="Исходный PNG; по умолчанию icon.png")
    create_icon(parser.parse_args().source)
