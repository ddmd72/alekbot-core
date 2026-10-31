"""A chunk of `tests/unit/adapters/firestore_drive_chunk_fakes.chunk` that carries its connection (MULTI_DRIVE_RFC §8):
the connection id is the user id, as `U_SCOPE` (`tests/unit/services/drive_index_fakes.py`) has it, so a write passes
the repositories' connection check and a read under `U_SCOPE` finds it. Technical ids only."""
from dataclasses import replace

from src.domain.drive_index import DriveChunk

from tests.unit.adapters import firestore_drive_chunk_fakes


def chunk(**kw) -> DriveChunk:
    """`firestore_drive_chunk_fakes.chunk(**kw)` with `connection_id` = its user id."""
    made = firestore_drive_chunk_fakes.chunk(**kw)
    return replace(made, connection_id=made.user_id)
