"""Rolling baselines and risk flags for the morning check-in."""

from __future__ import annotations

import argparse
import csv
import statistics
from datetime import date, datetime
from pathlib import Path

DEFAULT_CSV = Path(__file__).resolve().parent.parent / "monitoring" / "data" / "metrics.csv"
NUMERIC_FIELDS = ("sleep_hours", "hrv", "resting_hr", "feel")
BASELINE_DAYS = 60
SLEEP_WINDOW = 7
HRV_STREAK_DAYS = 3
RHR_STREAK_DAYS = 2
RHR_DELTA = 5.0
HRV_DROP_RATIO = 0.8
SHORT_SLEEP = 6.0
LOW_SLEEP_AVG = 6.5


def parse_date(value: str) -> date | None:
    """Parse an ISO date, returning None when the value is unusable."""
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_number(value: str) -> float | None:
    """Parse a decimal written with a dot or a comma, returning None when empty."""
    text = (value or "").strip().replace(",", ".")
    if not text:
        return None
    try:
        return round(float(text), 2)
    except ValueError:
        return None


def load(path: Path) -> list[dict]:
    """Load metrics rows from a CSV file, oldest first."""
    rows = []
    with open(path, newline="", encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            day = parse_date(raw.get("date", ""))
            if day is None:
                continue
            row = {"date": day, "note": (raw.get("note") or "").strip()}
            for field in NUMERIC_FIELDS:
                row[field] = parse_number(raw.get(field, ""))
            rows.append(row)
    return sorted(rows, key=lambda item: item["date"])


def baseline(rows: list[dict], field: str, window: int = BASELINE_DAYS) -> float | None:
    """Median of a field over the trailing window of the given rows."""
    values = [row[field] for row in rows if row[field] is not None][-window:]
    return statistics.median(values) if values else None


def streak_below(rows: list[dict], field: str, limit: float, days: int) -> bool:
    """True when the field stays below limit on every one of the last `days` rows with data."""
    seen = 0
    for row in reversed(rows):
        value = row[field]
        if value is None:
            continue
        if value >= limit:
            return False
        seen += 1
        if seen >= days:
            return True
    return False


def streak_above(rows: list[dict], field: str, limit: float, days: int) -> bool:
    """True when the field stays at or above limit on every one of the last `days` rows with data."""
    seen = 0
    for row in reversed(rows):
        value = row[field]
        if value is None:
            continue
        if value < limit:
            return False
        seen += 1
        if seen >= days:
            return True
    return False


def evaluate(rows: list[dict], today: date) -> dict:
    """Collect today's values, their baselines and the active flags."""
    report = {"today": today, "count": len(rows), "flags": [], "missing": []}
    if not rows:
        report["flags"].append("файл пуст")
        return report

    latest = rows[-1]
    history = rows[:-1]
    report["latest"] = latest
    report["days_ago"] = (today - latest["date"]).days

    for field in NUMERIC_FIELDS:
        report[f"{field}_base"] = baseline(history, field)
        if latest[field] is None:
            report["missing"].append(field)

    sleep_values = [row["sleep_hours"] for row in rows if row["sleep_hours"] is not None]
    report["sleep_avg"] = round(statistics.mean(sleep_values[-SLEEP_WINDOW:]), 1) if sleep_values else None

    if latest["date"] != today or report["missing"]:
        report["flags"].append("нет полных данных за сегодня")

    hrv_base = report["hrv_base"]
    if hrv_base and latest["hrv"] is not None:
        if streak_below(rows, "hrv", hrv_base, HRV_STREAK_DAYS):
            report["flags"].append(f"HRV ниже базы {HRV_STREAK_DAYS} дня подряд")
        if latest["hrv"] < hrv_base * HRV_DROP_RATIO:
            drop = round((1 - latest["hrv"] / hrv_base) * 100)
            report["flags"].append(f"HRV сегодня на {drop}% ниже базы")

    rhr_base = report["resting_hr_base"]
    if rhr_base and latest["resting_hr"] is not None:
        if streak_above(rows, "resting_hr", rhr_base + RHR_DELTA, RHR_STREAK_DAYS):
            report["flags"].append(f"пульс покоя выше базы на {RHR_DELTA:.0f} уже {RHR_STREAK_DAYS} дня")

    if latest["sleep_hours"] is not None and latest["sleep_hours"] < SHORT_SLEEP:
        report["flags"].append(f"сон короче {SHORT_SLEEP:.0f} ч")

    if report["sleep_avg"] is not None and report["sleep_avg"] < LOW_SLEEP_AVG:
        report["flags"].append(f"средний сон за {SLEEP_WINDOW} дней {report['sleep_avg']} ч")

    return report


def format_report(report: dict) -> str:
    """Render the report as a short plain-text block."""
    lines = []
    latest = report.get("latest")
    if latest is None:
        return "данных нет"

    stale = report["days_ago"]
    head = f"последняя запись {latest['date'].isoformat()}"
    if stale:
        head += f" ({stale} дн. назад)"
    lines.append(f"записей: {report['count']}; {head}")

    sleep = latest["sleep_hours"]
    lines.append(f"сон {_fmt(sleep)} ч (7д: {_fmt(report['sleep_avg'])})")
    lines.append(_metric_line("HRV", latest["hrv"], report["hrv_base"], "мс"))
    lines.append(_metric_line("пульс покоя", latest["resting_hr"], report["resting_hr_base"], ""))
    feel = latest["feel"]
    lines.append(f"самочувствие {_fmt(feel)}/5")

    if report["missing"]:
        lines.append(f"нет полей: {', '.join(report['missing'])}")

    lines.append("")
    if report["flags"]:
        lines.append("флаги:")
        lines.extend(f"- {flag}" for flag in report["flags"])
    else:
        lines.append("флаги: нет")
    return "\n".join(lines)


def _fmt(value: float | None) -> str:
    if value is None:
        return "нет"
    return f"{value:g}"


def _metric_line(name: str, value: float | None, base: float | None, unit: str) -> str:
    if value is None:
        return f"{name}: нет данных"
    tail = f" {unit}" if unit else ""
    if base is None:
        return f"{name} {_fmt(value)}{tail} (база: мало данных)"
    delta = round(value - base, 1)
    sign = "+" if delta > 0 else ""
    return f"{name} {_fmt(value)}{tail} (база 60д: {_fmt(base)}, {sign}{delta:g})"


def main() -> None:
    """Print the check-in report for today."""
    parser = argparse.ArgumentParser(description="Сводка по метрикам для утреннего чек-ина")
    parser.add_argument("--path", default=str(DEFAULT_CSV), help="путь к CSV с метриками")
    parser.add_argument("--today", help="дата чек-ина в формате YYYY-MM-DD")
    args = parser.parse_args()

    path = Path(args.path).expanduser()
    if not path.exists():
        raise SystemExit(f"нет файла {path}")

    today = parse_date(args.today) or date.today()
    print(format_report(evaluate(load(path), today)))


if __name__ == "__main__":
    main()
