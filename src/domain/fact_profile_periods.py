"""A quarter or a season printed in a fact, read as a range of months (fact profile RFC §3.3, owner decision).

The profiler stores such a source as a month-precision time point with `from` and `to`; this module says which months
the source stands for, so the date check accepts exactly the first and the last of them.

- A quarter ("Q1 2027", "1st quarter 2027", "T2 2026", "primer trimestre 2027", "перший квартал 2027") is its three
  calendar months: Q1 Jan-Mar, Q2 Apr-Jun, Q3 Jul-Sep, Q4 Oct-Dec.
- A season is the meteorological season of the northern hemisphere: spring Mar-May, summer Jun-Aug, autumn (fall)
  Sep-Nov, winter Dec-Feb. Winter crosses the year and is named by the year it starts in: "winter 2026" is
  Dec 2026 to Feb 2027.

Languages are those the date checker reads (English, Spanish, French, Ukrainian, Russian).
"""
import re
from typing import List, Tuple

_ORDINAL_WORDS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4,
    "primer": 1, "primero": 1, "segundo": 2, "tercer": 3, "tercero": 3, "cuarto": 4,
    "premier": 1, "deuxième": 2, "troisième": 3, "quatrième": 4,
    "перший": 1, "другий": 2, "третій": 3, "четвертий": 4,
    "первый": 1, "второй": 2, "третий": 3, "четвёртый": 4, "четвертый": 4,
}
_QUARTER_WORD = r"(?:quarter|trimestre|trim\.?|квартал)"
_YEAR_OF = r"(?:(?:of|de|del|в)\s+)?(\d{4})(?!\d)"
_QUARTER_CODE = re.compile(rf"(?<![\w])[QTК]\s?([1-4])\s*[/ ]?\s*{_YEAR_OF}", re.IGNORECASE)
_QUARTER_NUM = re.compile(rf"(?<![\w])([1-4])(?:st|nd|rd|th|er|e|ª|º|-?й)?\s+{_QUARTER_WORD}\s+{_YEAR_OF}",
                          re.IGNORECASE)
_QUARTER_WORDS = re.compile(rf"(?<![\w])({'|'.join(_ORDINAL_WORDS)})\s+{_QUARTER_WORD}\s+{_YEAR_OF}", re.IGNORECASE)
_QUARTER_THEN_NUM = re.compile(rf"(?<![\w]){_QUARTER_WORD}\s+([1-4])\s+{_YEAR_OF}", re.IGNORECASE)

# first month of the season; winter runs on into the next year
_SEASONS = {
    3: ("spring", "primavera", "printemps", "весна", "весни", "навесні", "весною", "весну", "весной"),
    6: ("summer", "verano", "été", "ete", "літо", "літа", "влітку", "лето", "лета", "летом"),
    9: ("autumn", "fall", "otoño", "automne", "осінь", "осені", "восени", "осень", "осенью"),
    12: ("winter", "invierno", "hiver", "зима", "зими", "взимку", "зимой", "зимы"),
}
_SEASON_START = {form: month for month, forms in _SEASONS.items() for form in forms}
_SEASON = re.compile(rf"(?<![\w])({'|'.join(sorted(map(re.escape, _SEASON_START), key=len, reverse=True))})\s+"
                     rf"{_YEAR_OF}", re.IGNORECASE)


def _months(year: int, first: int, count: int = 3) -> Tuple[str, str]:
    """(first, last) month as YYYY-MM of `count` months from `first`, crossing into the next year when needed."""
    last = first + count - 1
    end_year, end_month = (year + 1, last - 12) if last > 12 else (year, last)
    return f"{year}-{first:02d}", f"{end_year}-{end_month:02d}"


def period_ranges(source: str) -> List[Tuple[str, str]]:
    """The (first, last) month, as YYYY-MM, of every quarter or season printed in `source`."""
    found: List[Tuple[str, str]] = []
    for pattern in (_QUARTER_CODE, _QUARTER_NUM, _QUARTER_THEN_NUM):
        for number, year in pattern.findall(source):
            found.append(_months(int(year), (int(number) - 1) * 3 + 1))
    for word, year in _QUARTER_WORDS.findall(source):
        found.append(_months(int(year), (_ORDINAL_WORDS[word.casefold()] - 1) * 3 + 1))
    for word, year in _SEASON.findall(source):
        found.append(_months(int(year), _SEASON_START[word.casefold()]))
    return found
