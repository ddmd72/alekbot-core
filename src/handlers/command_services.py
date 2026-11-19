"""The services behind the owner's `$` commands, given to `ConversationHandler` as one frozen bundle.

Each is optional: a command whose service is absent answers that it is unavailable. Grouped so the handler's constructor
stays under the arity cap (`tests/unit/architecture/test_arch_constructor_arity_cap.py`); built in
`composition/chat_services.py`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ..services.drive_note_service import DriveNoteService
    from ..services.geo_mark_service import GeoMarkService
    from ..services.memory_fact_service import MemoryFactService
    from ..services.skill_service import SkillService


@dataclass(frozen=True)
class CommandServices:
    skills: Optional["SkillService"] = None              # `$skill`, and the skill preview delivery
    memory_facts: Optional["MemoryFactService"] = None   # `$memory save`
    drive_notes: Optional["DriveNoteService"] = None     # `$drive-notes` (DRIVE_FILE_NOTES_RFC §4.8); None with notes off
    geo_marks: Optional["GeoMarkService"] = None         # `$geo` and the Telegram pin (GEO_MARKS_RFC §4.5, §4.6)
