#!/usr/bin/env python3
"""Интерактивная генерация PDF-чеков из CSV/JSON и HTML-шаблонов."""

from __future__ import annotations

import csv
import json
import os
import platform
import subprocess
import sys
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from urllib.request import pathname2url

try:
    import pandas as pd
except ImportError:
    pd = None

from jinja2 import Environment, FileSystemLoader, select_autoescape
from weasyprint import CSS, HTML


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
TEMPLATES_DIR = ROOT / "templates"
OUTPUT_DIR = ROOT / "output"
FONTS_DIR = ROOT / "fonts"

INVOICE_ID_KEYS = ("invoice_id", "invoiceId", "InvoiceID", "id")


def configure_stdio() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stdin.reconfigure(encoding="utf-8")
        except Exception:
            pass


MONTHS_GENITIVE = (
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)

ONES_MASC = ("", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять")
ONES_FEM = ("", "одна", "две", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять")
TEENS = (
    "десять",
    "одиннадцать",
    "двенадцать",
    "тринадцать",
    "четырнадцать",
    "пятнадцать",
    "шестнадцать",
    "семнадцать",
    "восемнадцать",
    "девятнадцать",
)
TENS = ("", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят", "девяносто")
HUNDREDS = ("", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот")


def money(value: Any) -> str:
    amount = to_decimal(value)
    quantized = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    formatted = f"{quantized:,.2f}".replace(",", " ")
    return formatted


def format_qty(value: Any) -> str:
    amount = to_decimal(value)
    if amount == amount.to_integral_value():
        return str(int(amount))
    return format(amount.normalize(), "f").replace(".", ",")


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    n = n % 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def _triplet(n: int, feminine: bool = False) -> str:
    ones = ONES_FEM if feminine else ONES_MASC
    hundreds, rem = divmod(n, 100)
    parts: list[str] = []
    if hundreds:
        parts.append(HUNDREDS[hundreds])
    if 10 <= rem <= 19:
        parts.append(TEENS[rem - 10])
    else:
        tens, unit = divmod(rem, 10)
        if tens:
            parts.append(TENS[tens])
        if unit:
            parts.append(ones[unit])
    return " ".join(parts)


def amount_in_words(value: Any) -> str:
    amount = to_decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    rubles = int(amount)
    kopecks = int((amount - Decimal(rubles)) * 100)
    if rubles == 0:
        words = "ноль"
    else:
        groups = (
            (rubles // 1_000_000, False, "миллион", "миллиона", "миллионов"),
            ((rubles // 1_000) % 1_000, True, "тысяча", "тысячи", "тысяч"),
            (rubles % 1_000, False, "", "", ""),
        )
        chunks: list[str] = []
        for number, feminine, one, few, many in groups:
            if not number:
                continue
            chunk = _triplet(number, feminine)
            if one:
                chunk = f"{chunk} {_plural(number, one, few, many)}"
            chunks.append(chunk)
        words = " ".join(chunks)
    ruble_word = _plural(rubles, "рубль", "рубля", "рублей")
    kopeck_word = _plural(kopecks, "копейка", "копейки", "копеек")
    return f"{words} {ruble_word} {kopecks:02d} {kopeck_word}".capitalize()


def parse_act_date(value: Any) -> tuple[str, str, str, str]:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            dt = datetime.strptime(text[:10], fmt)
            return f"{dt.day:02d}", MONTHS_GENITIVE[dt.month], str(dt.year), dt.strftime("%d.%m.%Y")
        except ValueError:
            continue
    return "", "", text, text


def to_decimal(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    text = str(value).strip().replace(" ", "").replace(",", ".")
    try:
        return Decimal(text)
    except Exception:
        return Decimal("0")


def prompt_choice(title: str, options: list[Path] | list[str], label: str | None = None) -> Any:
    print()
    print(title)
    print("-" * len(title))
    for index, option in enumerate(options, start=1):
        display = option.name if isinstance(option, Path) else str(option)
        extra = f"  ({label})" if label else ""
        print(f"  {index}. {display}{extra}")
    print()

    while True:
        raw = input(f"Выберите номер (1-{len(options)}): ").strip()
        if raw.isdigit():
            number = int(raw)
            if 1 <= number <= len(options):
                return options[number - 1]
        print("Некорректный выбор. Введите номер из списка.")


def list_data_files() -> list[Path]:
    if not DATA_DIR.is_dir():
        return []
    files = [
        path
        for path in sorted(DATA_DIR.iterdir())
        if path.is_file() and path.suffix.lower() in {".csv", ".json"}
    ]
    return files


def list_templates() -> list[Path]:
    if not TEMPLATES_DIR.is_dir():
        return []
    return [
        path
        for path in sorted(TEMPLATES_DIR.iterdir())
        if path.is_file() and path.suffix.lower() in {".html", ".htm"}
    ]


def pick_invoice_id(record: dict[str, Any]) -> str | None:
    for key in INVOICE_ID_KEYS:
        if key in record and record[key] not in (None, ""):
            return str(record[key]).strip()
    return None


def normalize_item(raw: dict[str, Any]) -> dict[str, Any]:
    name = (
        raw.get("item_name")
        or raw.get("name")
        or raw.get("title")
        or raw.get("description")
        or "Позиция"
    )
    quantity = to_decimal(raw.get("quantity") or raw.get("qty") or 1)
    unit_price = to_decimal(raw.get("unit_price") or raw.get("price") or raw.get("amount") or 0)
    unit = raw.get("unit") or raw.get("ед") or raw.get("uom") or "шт"
    return {
        **raw,
        "item_name": str(name),
        "quantity": quantity,
        "unit": str(unit),
        "unit_price": unit_price,
        "line_total": quantity * unit_price,
        "quantity_display": format_qty(quantity),
    }


def merge_invoice(base: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    invoice = dict(base)
    item_keys = {
        "item_name",
        "name",
        "title",
        "description",
        "quantity",
        "qty",
        "unit_price",
        "price",
        "amount",
        "line_total",
    }
    has_item = any(row.get(key) not in (None, "") for key in ("item_name", "name", "title", "description"))
    if has_item:
        items = list(invoice.get("items") or [])
        items.append(normalize_item(row))
        invoice["items"] = items

    for key, value in row.items():
        if key in item_keys:
            continue
        if key not in invoice or invoice[key] in (None, ""):
            invoice[key] = value
    return invoice


def records_from_csv(path: Path) -> dict[str, dict[str, Any]]:
    invoices: dict[str, dict[str, Any]] = {}

    if pd is not None:
        frame = pd.read_csv(path, dtype=str).fillna("")
        rows = frame.to_dict(orient="records")
    else:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))

    for row in rows:
        cleaned = {str(k).strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k}
        invoice_id = pick_invoice_id(cleaned)
        if not invoice_id:
            continue
        invoices[invoice_id] = merge_invoice(invoices.get(invoice_id, {"invoice_id": invoice_id}), cleaned)
    return invoices


def records_from_json(path: Path) -> dict[str, dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig") as handle:
        payload = json.load(handle)

    if isinstance(payload, dict) and "invoices" in payload:
        payload = payload["invoices"]

    invoices: dict[str, dict[str, Any]] = {}

    if isinstance(payload, dict):
        # {"INV-1": {...}} or a single invoice object
        if pick_invoice_id(payload):
            payload = [payload]
        else:
            for key, value in payload.items():
                if isinstance(value, dict):
                    value = {**value, "invoice_id": value.get("invoice_id") or key}
                    invoices.update(records_from_iterable([value]))
            return invoices

    if isinstance(payload, list):
        return records_from_iterable(payload)

    raise ValueError(f"Неподдерживаемый формат JSON: {path.name}")


def records_from_iterable(items: list[Any]) -> dict[str, dict[str, Any]]:
    invoices: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        invoice_id = pick_invoice_id(item)
        if not invoice_id:
            continue
        record = dict(item)
        record["invoice_id"] = invoice_id
        raw_items = record.get("items")
        if isinstance(raw_items, list):
            record["items"] = [normalize_item(x) if isinstance(x, dict) else normalize_item({"item_name": x}) for x in raw_items]
        else:
            record = merge_invoice({"invoice_id": invoice_id}, record)
        invoices[invoice_id] = merge_invoice(invoices.get(invoice_id, {"invoice_id": invoice_id}), record)
    return invoices


def load_invoices(path: Path) -> dict[str, dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        invoices = records_from_csv(path)
    elif suffix == ".json":
        invoices = records_from_json(path)
    else:
        raise ValueError(f"Неподдерживаемый тип файла: {path.name}")
    if not invoices:
        raise ValueError(f"В файле {path.name} не найдено ни одного invoice id.")
    return invoices


def finalize_context(record: dict[str, Any]) -> dict[str, Any]:
    context = dict(record)
    items = [normalize_item(item) if isinstance(item, dict) else normalize_item({"item_name": item}) for item in context.get("items") or []]
    if not items:
        items = [normalize_item(context)]

    subtotal = sum((item["line_total"] for item in items), Decimal("0"))
    tax_rate = to_decimal(context.get("tax_rate") or context.get("vat") or 0)
    tax = (subtotal * tax_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    total = subtotal + tax
    day, month, year, date_dotted = parse_act_date(context.get("date") or "")

    executor_name = (
        context.get("executor_name")
        or context.get("contractor_name")
        or context.get("seller_name")
        or "ООО"
    )
    executor_title = context.get("executor_title") or "Генеральный директор"
    basis = context.get("basis") or context.get("основание") or context.get("notes") or ""

    context.update(
        {
            "items": items,
            "items_count": len(items),
            "subtotal": subtotal,
            "tax": tax,
            "tax_rate": tax_rate,
            "total": total,
            "total_words": amount_in_words(total),
            "currency": context.get("currency") or "RUB",
            "customer_name": context.get("customer_name") or context.get("customer") or "",
            "customer_email": context.get("customer_email") or "",
            "customer_address": context.get("customer_address") or "",
            "date": context.get("date") or "",
            "date_day": day,
            "date_month": month,
            "date_year": year,
            "date_dotted": date_dotted,
            "notes": context.get("notes") or "",
            "basis": basis,
            "executor_name": executor_name,
            "executor_title": executor_title,
        }
    )
    return context


def find_font(names: tuple[str, ...]) -> Path | None:
    for name in names:
        candidate = FONTS_DIR / name
        if candidate.is_file():
            return candidate
    return None


def file_url(path: Path) -> str:
    return urljoin("file:", pathname2url(str(path.resolve())))


def font_css() -> CSS:
    regular = find_font(("Roboto-Regular.ttf", "DejaVuSans.ttf", "roboto.ttf"))
    bold = find_font(("Roboto-Bold.ttf", "DejaVuSans-Bold.ttf", "Roboto-Regular.ttf", "DejaVuSans.ttf"))
    if regular is None:
        raise FileNotFoundError(
            "Не найден шрифт в папке fonts/. Положите туда Roboto-Regular.ttf или DejaVuSans.ttf."
        )

    regular_url = file_url(regular)
    bold_url = file_url(bold or regular)
    css = f"""
    @font-face {{
      font-family: "Roboto";
      src: url("{regular_url}");
      font-weight: 400;
      font-style: normal;
    }}
    @font-face {{
      font-family: "Roboto";
      src: url("{bold_url}");
      font-weight: 700;
      font-style: normal;
    }}
    @font-face {{
      font-family: "DejaVu Sans";
      src: url("{regular_url}");
      font-weight: 400;
      font-style: normal;
    }}
    @font-face {{
      font-family: "DejaVu Sans";
      src: url("{bold_url}");
      font-weight: 700;
      font-style: normal;
    }}
    html, body {{
      font-family: "Roboto", "DejaVu Sans", sans-serif;
    }}
    """
    return CSS(string=css)


def render_pdf(template_path: Path, context: dict[str, Any], output_path: Path) -> None:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    env.filters["money"] = money
    env.filters["qty"] = format_qty
    env.filters["amount_in_words"] = amount_in_words
    html = env.get_template(template_path.name).render(**context)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    HTML(string=html, base_url=str(TEMPLATES_DIR)).write_pdf(output_path, stylesheets=[font_css()])


def open_pdf(path: Path) -> None:
    system = platform.system()
    if system == "Windows":
        os.startfile(path)  # type: ignore[attr-defined]
    elif system == "Darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


def print_banner() -> None:
    print("=" * 46)
    print("  Генератор PDF-чеков")
    print("=" * 46)


def main() -> int:
    configure_stdio()
    print_banner()

    data_files = list_data_files()
    templates = list_templates()

    print()
    print("Доступные файлы с данными:")
    if data_files:
        for index, path in enumerate(data_files, start=1):
            print(f"  {index}. {path.name}")
    else:
        print("  (нет CSV/JSON в папке data/)")

    print()
    print("Доступные шаблоны:")
    if templates:
        for index, path in enumerate(templates, start=1):
            print(f"  {index}. {path.name}")
    else:
        print("  (нет HTML в папке templates/)")

    if not data_files or not templates:
        print()
        print("Добавьте файлы в data/ и templates/, затем запустите скрипт снова.")
        return 1

    data_path = prompt_choice("Выбор файла данных", data_files)
    template_path = prompt_choice("Выбор HTML-шаблона", templates)

    try:
        invoices = load_invoices(data_path)
    except Exception as exc:
        print(f"Не удалось прочитать данные: {exc}")
        return 1

    invoice_ids = sorted(invoices.keys())
    invoice_id = prompt_choice("Доступные чеки (invoice id)", invoice_ids)
    context = finalize_context(invoices[invoice_id])

    safe_id = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in invoice_id)
    output_path = OUTPUT_DIR / f"{safe_id}.pdf"

    try:
        render_pdf(template_path, context, output_path)
    except Exception as exc:
        print(f"Ошибка генерации PDF: {exc}")
        print("На Windows для WeasyPrint обычно нужны GTK3 runtime (Pango/Cairo).")
        return 1

    print()
    print(f"PDF сохранён: {output_path}")
    open_pdf(output_path)
    print("Документ открыт в системной программе.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
