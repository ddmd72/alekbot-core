"""
ContainerFactsPort — the facts of a ZIP container (member count, macro parts), read where untrusted bytes are parsed
(docs/10_rfcs/EXTERNAL_FILES_RFC.md D13; owner decision O2 of 2026-11-05: in the sandbox, never in the main service).

`send_file_to_user` refuses an email attachment that is an Office or ODF document with macros; the main service only
compares header bytes and never lists a ZIP itself. The sandbox's `SandboxContentExtractor` implements this port over
`parse.container_facts` (UNTRUSTED_CONTENT_SANDBOX_RFC §4.7); tests substitute a fake.

A run failure of the op (timeout, memory, an invalid result) is an answer, `ContainerFacts(status=ERROR)`, not an
exception. An adapter whose service cannot take the call raises `drive_extraction.ParserUnavailable` (systemic). Either
way the container is not checked, and the caller fails closed. `deadline_at` is absolute epoch seconds and can only
lower the adapter's own wall clock.
"""
from abc import ABC, abstractmethod
from typing import Optional

from src.domain.container_facts import ContainerFacts


class ContainerFactsPort(ABC):

    @abstractmethod
    async def container_facts(self, data: bytes, *, deadline_at: Optional[float] = None) -> ContainerFacts:
        """The facts of `data` read as a ZIP container (`NOT_ZIP` when it is not one)."""
