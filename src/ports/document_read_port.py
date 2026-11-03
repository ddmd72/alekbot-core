"""
DocumentReadPort — read one external file's bytes into an outcome (docs/10_rfcs/EXTERNAL_FILES_RFC.md §4.4, D1, P1).

The open path stages the bytes and calls `read`; the service kind-checks them, applies the caps, decides native or
pipeline (the parser in its child, the text-or-scan router, the reader) and returns a closed-schema `ReadOutcome`.
Marking and the presentation cut are the presenter's (§4.6), never the service's.

A port with one implementation today (P1): D1 names the second, a remote adapter, and the port gives test substitution
now.
"""
from abc import ABC, abstractmethod

from src.domain.document_read import ReadOutcome, ReadRequest


class DocumentReadPort(ABC):

    @abstractmethod
    async def read(self, request: ReadRequest) -> ReadOutcome:
        """The outcome of reading `request.content`. Every result is an outcome, outages and deadlines included
        (`MODEL_UNAVAILABLE`, `PARSER_UNAVAILABLE`, `TIMEOUT`); it never raises."""
