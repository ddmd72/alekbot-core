"""What the drive connector's paging lines of one `search_drive` or `find_passages` call need (DRIVE_MCP_CONNECTOR_RFC
§14.4): whether the call was `wide`, its batch, and the arguments the client sent, to quote back in the next-batch
line. The question and the scope are echoed to the client this way and never logged (§9). Pure.

Public interface:
    ConnectorPaging(wide, batch, arguments)
    ConnectorPaging.next_batch_arguments() -> str     the arguments of batch + 1, as `name="value"` pairs
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Tuple

WIDE_VALUE = "yes"


@dataclass(frozen=True)
class ConnectorPaging:
    """`arguments`: (name, value) in the tool's parameter order, as the call received them, `wide` and `batch` left
    out (this value carries them); an empty value is not quoted back."""
    wide: bool = False
    batch: int = 1
    arguments: Tuple[Tuple[str, str], ...] = ()

    def next_batch_arguments(self) -> str:
        """The same arguments with `batch` set to the next batch (and `wide` kept), each value a JSON string, so a
        quote inside the question cannot end it: `question="…", folder="R&D", wide="yes", batch="2"`."""
        pairs = [(name, value) for name, value in self.arguments if value]
        if self.wide:
            pairs.append(("wide", WIDE_VALUE))
        pairs.append(("batch", str(self.batch + 1)))
        return ", ".join(f"{name}={json.dumps(value, ensure_ascii=False)}" for name, value in pairs)
