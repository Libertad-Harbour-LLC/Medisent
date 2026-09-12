#!/usr/bin/env python3
"""Сборка коммерческого предложения в PDF по фирменному шаблону."""

import argparse
import base64
import datetime as dt
import json
import mimetypes
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"


def money(value, decimals=2):
    """1234567.5 -> '1 234 567,50' (неразрывные пробелы)."""
    s = f"{float(value):,.{decimals}f}"
    return s.replace(",", "\u00a0").replace(".", ",")


def data_uri(path):
    """PNG в base64 — иначе Chromium не подтянет файл при рендере из строки."""
    p = Path(path)
    if not p.is_absolute():
        p = ASSETS / p
    if not p.exists():
        return None
    mime = mimetypes.guess_type(p.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"


def load_json(path, what):
    p = Path(path)
    if not p.exists():
        sys.exit(f"Не найден {what}: {p}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"Битый JSON в {p}: {e}")


def compute(data, brand):
    """Считает суммы по позициям, скидку, НДС и итог."""
    items = data.get("items") or []
    if not items:
        sys.exit("В данных нет ни одной позиции (items).")

    rows = []
    subtotal = 0.0
    for i, it in enumerate(items, 1):
        try:
            qty = float(it.get("qty", 1))
            price = float(it["price"])
        except (KeyError, TypeError, ValueError):
            sys.exit(f"Позиция {i}: нет корректных qty/price -> {it}")
        total = qty * price
        subtotal += total
        rows.append({
            "n": i,
            "name": it.get("name", "—"),
            "note": it.get("note", ""),
            "qty": money(qty, 0) if float(qty).is_integer() else money(qty, 2),
            "unit": it.get("unit", "шт."),
            "price": money(price),
            "total": money(total),
        })

    discount_pct = float(data.get("discount_pct", 0))
    discount = subtotal * discount_pct / 100
    net = subtotal - discount

    vat_rate = float(data.get("vat_rate", brand.get("vat_rate", 0)))
    vat_included = bool(data.get("vat_included", brand.get("vat_included", True)))
    if vat_rate:
        vat = net * vat_rate / (100 + vat_rate) if vat_included else net * vat_rate / 100
        grand = net if vat_included else net + vat
    else:
        vat, grand = 0.0, net

    return {
        "rows": rows,
        "subtotal": money(subtotal),
        "discount_pct": f"{discount_pct:g}",
        "discount": money(discount),
        "has_discount": discount_pct > 0,
        "vat_rate": f"{vat_rate:g}",
        "vat": money(vat),
        "has_vat": vat_rate > 0,
        "vat_included": vat_included,
        "grand": money(grand),
        "currency": data.get("currency", brand.get("currency", "₽")),
    }


def render_html(data, brand, with_stamp):
    env = Environment(
        loader=FileSystemLoader(str(ASSETS)),
        autoescape=select_autoescape(["html"]),
    )
    today = dt.date.today()

    valid_until = data.get("valid_until")
    defaulted = False
    if not valid_until:
        valid_until = (today + dt.timedelta(days=14)).strftime("%d.%m.%Y")
        defaulted = True

    ctx = {
        "brand": brand,
        "kp": data,
        "totals": compute(data, brand),
        "date": data.get("date") or today.strftime("%d.%m.%Y"),
        "number": data.get("number") or f"КП-{today.strftime('%Y%m%d')}",
        "valid_until": valid_until,
        "logo": data_uri(brand.get("logo", "logo.png")),
        "stamp": data_uri(brand.get("stamp", {}).get("file", "stamp.png")) if with_stamp else None,
        "signature": data_uri(brand.get("signature", {}).get("file", "signature.png")) if with_stamp else None,
        "draft": not with_stamp,
    }
    return env.get_template("template.html").render(**ctx), defaulted


def to_pdf(html, out_path):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return weasy_fallback(html, out_path)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="load")
        page.pdf(
            path=str(out_path),
            format="A4",
            print_background=True,
            margin={"top": "18mm", "bottom": "16mm", "left": "16mm", "right": "16mm"},
        )
        browser.close()
    return "chromium"


def weasy_fallback(html, out_path):
    try:
        from weasyprint import HTML
    except ImportError:
        sys.exit(
            "Нет ни Playwright, ни WeasyPrint.\n"
            "  pip install playwright && playwright install chromium"
        )
    HTML(string=html, base_url=str(ASSETS)).write_pdf(str(out_path))
    return "weasyprint (печать без blend-режима)"


def main():
    ap = argparse.ArgumentParser(description="Сборка КП в PDF")
    ap.add_argument("--data", required=True, help="JSON с клиентом и позициями")
    ap.add_argument("--brand", default=str(ASSETS / "brand.json"))
    ap.add_argument("--out", default="kp.pdf")
    ap.add_argument("--no-stamp", action="store_true", help="черновик без печати и подписи")
    ap.add_argument("--html-only", action="store_true", help="сохранить HTML, не рендерить PDF")
    args = ap.parse_args()

    data = load_json(args.data, "файл данных")
    brand = load_json(args.brand, "файл реквизитов")

    html, defaulted = render_html(data, brand, with_stamp=not args.no_stamp)
    out = Path(args.out)

    if args.html_only:
        out = out.with_suffix(".html")
        out.write_text(html, encoding="utf-8")
        print(f"HTML: {out}")
        return

    engine = to_pdf(html, out)
    print(f"PDF: {out}  ({engine})")
    if args.no_stamp:
        print("Черновик: печати и подписи нет.")
    if defaulted:
        print("Срок действия не задан — поставлен +14 дней. Проверь.")


if __name__ == "__main__":
    main()
