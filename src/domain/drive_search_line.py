"""The `search` line of the drive specialist's state block (DRIVE_SPECIALIST_RFC §5.3), read over the same facts a
search reads (`search_gate`): the model knows up front what can work; the operations still carry today's text for
these states (§7.2)."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from .drive_index import MIGRATION_GATE_KEY, DriveSyncState, EpochPhase
from .drive_search import STATUS_DISABLED, STATUS_DISCONNECTED, search_gate
from .user_drive import DriveConnection


class SearchLine(str, Enum):
    ON = "on"
    OFF = "off (drive indexing not turned on; the deleted recordings and notes search still works)"
    REBUILDING = "rebuilding"
    BUILDING = "still building"


def search_line(enabled: bool, state: Optional[DriveSyncState], connection: DriveConnection,
                now: datetime) -> SearchLine:
    """The search line from the gate's facts. A drive that is not connected keeps `on`: the drive line says it."""
    gate = search_gate(enabled, state, connection, now)
    if gate.status == STATUS_DISABLED:
        return SearchLine.OFF
    if gate.status == STATUS_DISCONNECTED:
        return SearchLine.REBUILDING if gate.other_drive else SearchLine.ON
    if gate.rebuilding:
        return SearchLine.REBUILDING
    if state is None or not state.root_item_id:
        return SearchLine.BUILDING
    if MIGRATION_GATE_KEY not in state.applied_versions:
        return SearchLine.REBUILDING
    if state.epoch_phase is not EpochPhase.IDLE:
        return SearchLine.BUILDING
    return SearchLine.ON
