"""The connector's `wide` and `batch` arguments (DRIVE_MCP_CONNECTOR_RFC §14.2). Pure.

`wide` is liberal input: "yes", "true" or "1" in any case (spaces around ignored) asks for the wide caps; empty or any
other value is the default, never an error. `batch` is a positive whole number as a string, empty meaning 1; anything
else is a correctable request error, a `ValueError` whose message is the line the client reads (as
`drive_excerpt_request`'s refusals).

Public interface:
    parse_wide(value) -> bool
    parse_batch(value) -> int            raises ValueError(BAD_BATCH) for a value that is not a positive integer
    BAD_BATCH
"""
from __future__ import annotations

import re
from typing import Any

BAD_BATCH = ("'{value}' is not a batch number: batch is a whole number from 1 (empty is the first batch); ask for the "
             "next batch with the number a result's next-batch line gives.")

_WIDE_WORDS = frozenset({"yes", "true", "1"})
_BATCH = re.compile(r"\s*(\d+)\s*")


def parse_wide(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return bool(value)
    return str(value).strip().lower() in _WIDE_WORDS


def parse_batch(value: Any) -> int:
    if value is None:
        return 1
    if isinstance(value, bool):
        raise ValueError(BAD_BATCH.format(value=value))
    if isinstance(value, int):
        if value < 1:
            raise ValueError(BAD_BATCH.format(value=value))
        return value
    text = str(value)
    if not text.strip():
        return 1
    found = _BATCH.fullmatch(text)
    if found is None or int(found.group(1)) < 1:
        raise ValueError(BAD_BATCH.format(value=text.strip()))
    return int(found.group(1))
