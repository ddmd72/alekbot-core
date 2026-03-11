"""
Weather parser utility.

Extracts structured weather data from grounded web search responses.
"""
import re
from typing import Optional, Dict, List

WEATHER_KEYWORDS = {
    "weather", "forecast", "погода", "прогноз"
}

WEEKDAY_NAMES = [
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "mon", "tue", "wed", "thu", "fri", "sat", "sun",
    "today", "tomorrow"
]

DAY_ALIASES = {
    "mon": "Monday",
    "monday": "Monday",
    "tue": "Tuesday",
    "tuesday": "Tuesday",
    "wed": "Wednesday",
    "wednesday": "Wednesday",
    "thu": "Thursday",
    "thursday": "Thursday",
    "fri": "Friday",
    "friday": "Friday",
    "sat": "Saturday",
    "saturday": "Saturday",
    "sun": "Sunday",
    "sunday": "Sunday",
    "today": "Today",
    "tomorrow": "Tomorrow"
}

CONDITION_MAP = {
    "sunny": ["sunny", "clear", "ясно", "солнечно", "сонячно"],
    "cloudy": ["cloud", "overcast", "облачно", "хмарно"],
    "rainy": ["rain", "showers", "дождь", "дощ"],
    "snowy": ["snow", "снег", "сніг"],
    "storm": ["storm", "гроза"],
    "fog": ["fog", "туман"]
}


def is_weather_query(query: str) -> bool:
    normalized = query.lower()
    return any(keyword in normalized for keyword in WEATHER_KEYWORDS)


def extract_location(query: str) -> Optional[str]:
    normalized = query.strip()
    patterns = [
        r"weather\s+in\s+([^|]+)",
        r"weather\s+forecast\s+([^|]+?)(?:\s+next|\s+for|\s+\d+|$)",
        r"forecast\s+for\s+([^|]+?)(?:\s+next|\s+\d+|$)",
        r"погода\s+в\s+([^|]+)",
        r"прогноз\s+в\s+([^|]+)",
        r"Object:\s*([^|]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            value = match.group(1).strip()
            value = re.sub(r"\s+\|.*$", "", value).strip()
            return value
    return None


def detect_condition(text: str) -> str:
    lower = text.lower()
    for condition, keywords in CONDITION_MAP.items():
        if any(keyword in lower for keyword in keywords):
            return condition
    return "unknown"


def _extract_temperatures(text: str) -> List[int]:
    return [int(m.group(1)) for m in re.finditer(r"(-?\d{1,2})\s*°", text)]


def _extract_min_max(temps: List[int]) -> tuple[Optional[int], Optional[int]]:
    if len(temps) < 2:
        return None, None
    return min(temps), max(temps)


def _parse_structured_day_line(line: str) -> Optional[Dict[str, object]]:
    """Parse lines like: Monday: 7-15°C | cloudy | 55% | 25 km/h"""
    match = re.match(r"^(?P<day>[A-Za-z]+)\s*:\s*(?P<temps>[^|]+)\|(?P<rest>.+)$", line.replace(" / ", "-"))
    if not match:
        return None

    day_label = DAY_ALIASES.get(match.group("day").lower())
    if not day_label:
        return None

    temps_text = match.group("temps").strip().replace("/", "-")
    temp_result = _extract_temp_range(temps_text)
    if not temp_result:
        return None

    temp_range, unit = temp_result
    temps = _extract_temperatures(temps_text)
    if temps_text.lower().find("temp") != -1 and len(temps) == 1:
        temp_min, temp_max = None, None
    else:
        temp_min, temp_max = _extract_min_max(temps)

    rest = match.group("rest")
    humidity = _extract_humidity(rest)
    wind = _extract_wind(rest)
    condition = detect_condition(rest)

    return {
        "day": day_label,
        "temp": temp_range,
        "temp_min": f"{temp_min}°{unit}" if temp_min is not None else None,
        "temp_max": f"{temp_max}°{unit}" if temp_max is not None else None,
        "unit": unit,
        "wind": wind,
        "humidity": humidity,
        "condition": condition
    }


def _extract_temp_range(text: str) -> Optional[tuple[str, str]]:
    min_match = re.search(r"(?:min|low)\s*(-?\d{1,2})\s*°?\s*([CF])?", text, flags=re.IGNORECASE)
    max_match = re.search(r"(?:max|high)\s*(-?\d{1,2})\s*°?\s*([CF])?", text, flags=re.IGNORECASE)
    if min_match and max_match:
        min_val = int(min_match.group(1))
        max_val = int(max_match.group(1))
        unit = min_match.group(2) or max_match.group(2) or "C"
        return f"{min(min_val, max_val)}-{max(min_val, max_val)}°{unit}", unit

    matches = re.findall(r"(-?\d{1,2})\s*°\s*([CF])", text)
    if matches:
        temps_by_unit: Dict[str, List[int]] = {"C": [], "F": []}
        for value, unit in matches:
            temps_by_unit[unit].append(int(value))

        unit = "C" if temps_by_unit["C"] else "F"
        temps = temps_by_unit[unit]
        if len(temps) == 1:
            return f"{temps[0]}°{unit}", unit
        return f"{min(temps)}-{max(temps)}°{unit}", unit

    temps = _extract_temperatures(text)
    if not temps:
        return None

    unit = "C"
    if len(temps) == 1:
        return f"{temps[0]}°{unit}", unit

    low = min(temps)
    high = max(temps)
    return f"{low}-{high}°{unit}", unit


def _extract_wind(text: str) -> Optional[str]:
    match = re.search(r"(\d{1,3}(?:\.\d+)?)\s*(km/h|км/ч|m/s|м/с)", text, flags=re.IGNORECASE)
    if not match:
        return None
    value = match.group(1)
    unit = match.group(2)
    return f"{value} {unit}"


def _extract_humidity(text: str) -> Optional[str]:
    match = re.search(r"(\d{1,3})\s*%", text)
    if not match:
        return None
    return f"{match.group(1)}%"


def _detect_day_label(text: str) -> Optional[str]:
    lower = text.lower()
    for name in WEEKDAY_NAMES:
        if name in lower:
            return DAY_ALIASES.get(name, name.capitalize())
    return None


def _normalize_line(line: str) -> str:
    return re.sub(r"^[\s*\-•]+", "", line).strip()


def _parse_rows(text: str) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for raw_line in text.splitlines():
        line = _normalize_line(raw_line)
        if not line:
            continue

        structured = _parse_structured_day_line(line)
        if structured:
            rows.append(structured)
            continue

        day_label = _detect_day_label(line)
        if not day_label:
            continue

        temp_result = _extract_temp_range(line)
        if not temp_result:
            continue

        temp_range, unit = temp_result
        temps = _extract_temperatures(line)
        if line.lower().find("temp") != -1 and len(temps) == 1:
            temp_min, temp_max = None, None
        else:
            temp_min, temp_max = _extract_min_max(temps)

        row = {
            "day": day_label,
            "temp": temp_range,
            "temp_min": f"{temp_min}°{unit}" if temp_min is not None else None,
            "temp_max": f"{temp_max}°{unit}" if temp_max is not None else None,
            "unit": unit,
            "wind": _extract_wind(line),
            "humidity": _extract_humidity(line),
            "condition": detect_condition(line)
        }
        rows.append(row)

    return rows


def parse_weather(text: str, query: str) -> Optional[Dict[str, object]]:
    if not text:
        return None

    if not is_weather_query(query):
        return None

    rows = _parse_rows(text)
    if not rows:
        temp_result = _extract_temp_range(text)
        if not temp_result:
            return None
        temp_range, unit = temp_result
        temps = _extract_temperatures(text)
        if text.lower().find("temp") != -1 and len(temps) == 1:
            temp_min, temp_max = None, None
        else:
            temp_min, temp_max = _extract_min_max(temps)
        rows = [
            {
                "day": "Today",
                "temp": temp_range,
                "temp_min": f"{temp_min}°{unit}" if temp_min is not None else None,
                "temp_max": f"{temp_max}°{unit}" if temp_max is not None else None,
                "unit": unit,
                "wind": _extract_wind(text),
                "humidity": _extract_humidity(text),
                "condition": detect_condition(text)
            }
        ]

    rows_by_day: Dict[str, Dict[str, object]] = {}
    order: List[str] = []
    for row in rows:
        day = row.get("day")
        if not day:
            continue
        existing = rows_by_day.get(day)
        if not existing:
            rows_by_day[day] = row
            order.append(day)
            continue

        if existing.get("unit") != "C" and row.get("unit") == "C":
            existing["temp"] = row.get("temp")
            existing["temp_min"] = row.get("temp_min")
            existing["temp_max"] = row.get("temp_max")
            existing["unit"] = "C"

        if not existing.get("wind") and row.get("wind"):
            existing["wind"] = row.get("wind")
        if not existing.get("humidity") and row.get("humidity"):
            existing["humidity"] = row.get("humidity")
        if existing.get("condition") == "unknown" and row.get("condition") != "unknown":
            existing["condition"] = row.get("condition")

    rows = [rows_by_day[day] for day in order]

    location = extract_location(query) or "Unknown location"
    any_wind = any(row.get("wind") for row in rows)
    any_humidity = any(row.get("humidity") for row in rows)

    headers = ["Day", "Night", "Daytime", "Condition", "Humidity", "Wind"]

    table_rows: List[Dict[str, object]] = []
    for row in rows:
        temp_min = row.get("temp_min")
        temp_max = row.get("temp_max")
        temp_range = row.get("temp")

        night_value = temp_min or "—"
        day_value = temp_max or "—"

        if not temp_min and not temp_max and temp_range:
            night_value = temp_range
            day_value = "—"

        cells = [
            row.get("day", "—"),
            night_value,
            day_value,
            row.get("condition", "unknown"),
            row.get("humidity") or "—",
            row.get("wind") or "—"
        ]

        table_rows.append({
            "cells": cells,
            "metadata": {"condition": row.get("condition", "unknown")}
        })

    footer = "Дані з кількох джерел" if "Source" in text else None

    return {
        "title": f"Прогноз погоди в {location}",
        "headers": headers,
        "rows": table_rows,
        "footer": footer
    }
