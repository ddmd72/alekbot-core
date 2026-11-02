"""Two indexed drives for the cross-drive search tests (multi-drive 2b P-C): `Drive` (connection `u`, tag `a1`, the
primary) and `Acme` (connection `conn-2`, tag `b2`), each with records of its own. `on_acme` builds a record with the
fixture's builders and moves it to Acme's index."""
from __future__ import annotations

from dataclasses import replace
from typing import Optional

from src.domain.drive_content_identity import ContentIdentity

from .drive_mounts_harness import MountedWorld

ACME_ROOT = "root2"
ACME_CONN = "conn-2"


class TwoDrives(MountedWorld):
    def on_acme(self, item: str, *, identity: Optional[ContentIdentity] = None) -> str:
        """Move the record `item` (and its vectors, chunks, hints) built under `Drive` to Acme's index."""
        repo = self.repo
        rec = repo.records.pop(("u", item))
        rec = replace(rec, connection_id=ACME_CONN, parent_id=ACME_ROOT if rec.parent_id == "root" else rec.parent_id)
        if identity is not None:
            rec = replace(rec, content_identity=identity)
        repo.records[(ACME_CONN, item)] = rec
        repo.vectors[(ACME_CONN, item)] = repo.vectors.pop(("u", item))
        for key in [k for k in repo.chunks if k[0] == "u" and k[1] == item]:
            chunk, vector = repo.chunks.pop(key)
            repo.chunks[(ACME_CONN, *key[1:])] = (replace(chunk, connection_id=ACME_CONN), vector)
        for key in [k for k in repo.hints if k[0] == "u" and k[1] == item]:
            doc, vector = repo.hints.pop(key)
            repo.hints[(ACME_CONN, *key[1:])] = (replace(doc, connection_id=ACME_CONN), vector)
        return item
