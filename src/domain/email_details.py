"""
What `EmailSearchService.get_details_with_parts` returns (EXTERNAL_FILES_RFC §4.7 P8b, D11). Pure.

`text` is the answer the model reads (the body inside the untrusted wrapper, the attachment labels after it). `files`
are the same attachments as `(label, ref)` pairs, the shape of the `email_file_context` history entries
(`email_ref.EMAIL_FILE_CONTEXT_KEY`), so the caller can keep them for the next turn without parsing `text`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class EmailDetails:
    text: str
    files: Tuple[Tuple[str, str], ...] = ()
