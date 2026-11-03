"""The specialist hands the native-format declaration it received to the open call unchanged (U2 A5)."""
from __future__ import annotations

from tests.unit.infrastructure.test_drive_tools_open_bound import _run, _call, _whole, tt
from src.domain.native_format import NATIVE_FORMATS_KEY

DECLARATION = [{"mime": "application/pdf", "max_bytes": 1000, "max_pages": 5}]


async def test_open_receives_the_declaration_from_the_specialist_context():
    context = {"user_id": "u1", "account_id": "a1", NATIVE_FORMATS_KEY: DECLARATION}
    ops, run, _ = _run(_whole(), context)
    await _call(run, tt.OPEN_DRIVE_FILE, file_ref="drive:a1:item-1")
    assert ops.open.call_args.args[0][NATIVE_FORMATS_KEY] == DECLARATION

