#!/usr/bin/env python3
"""Проверяет логотип, печать и подпись: прозрачность, размер, белую подложку."""

import sys
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "assets"

# подпись всегда широкая и низкая — по высоте её не меряем
CHECKS = {
    "logo.png":      {"min_w": 300, "min_h": 120, "need_alpha": True},
    "stamp.png":     {"min_w": 600, "min_h": 600, "need_alpha": True},
    "signature.png": {"min_w": 600, "min_h": 120, "need_alpha": True},
}


def main():
    try:
        from PIL import Image
    except ImportError:
        sys.exit("Нужен Pillow:  pip install pillow")

    problems = 0
    for name, rule in CHECKS.items():
        path = ASSETS / name
        if not path.exists():
            print(f"[нет]  {name} — файла нет, документ соберётся без него")
            problems += 1
            continue

        img = Image.open(path)
        w, h = img.size
        issues = []

        if w < rule["min_w"] or h < rule["min_h"]:
            issues.append(f"мелкий ({w}×{h}, нужно от {rule['min_w']}×{rule['min_h']} px)")

        if rule["need_alpha"] and img.mode not in ("RGBA", "LA", "P"):
            issues.append("нет альфа-канала — фон ляжет белым прямоугольником")
        elif img.mode == "RGBA":
            alpha = img.getchannel("A")
            opaque = sum(alpha.histogram()[250:])
            if opaque / (w * h) > 0.92:
                issues.append("альфа есть, но почти всё непрозрачно — похоже, фон не вычищен")

        if issues:
            print(f"[!]    {name}: " + "; ".join(issues))
            problems += 1
        else:
            print(f"[ок]   {name} — {w}×{h}, {img.mode}")

    print()
    if problems:
        print(f"Проблем: {problems}. Как готовить файлы — references/stamping.md")
        sys.exit(1)
    print("Всё готово к сборке.")


if __name__ == "__main__":
    main()
