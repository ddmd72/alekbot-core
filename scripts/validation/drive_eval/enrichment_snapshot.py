"""
The recorded enrichment snapshot (USER_DRIVE_INDEXING_RFC §25.7, F-22): `tests/fixtures/drive_eval/enrichment.json`.

It holds what the model calls of one live indexing run of the synthetic fixture answered, so the Search v2 eval and
search tests replay the index with no model call (`enrichment_replay.py`) and the search is developed and
regression-tested deterministically. Written once by `dump_enrichment.py --fixture` (owner-run, paid); a re-record
is a deliberate commit.

Format (`format` 1), every map keyed by what the answer depends on, so a replay finds the answer for the same input
whatever item, generation or batch position asks:
- `profiles`: `profile_key(request)` (kind, name, the profiler input and the previous labels) → `{path, status,
  profile}`; `profile` is the model's output **before** `drive_profile.verify` (the indexer verifies at replay).
- `hints`: `hint_key(chunk text)` → `{path, index, status, items}`; `items` are the model's items, ungrounded (the
  indexer grounds them at replay).
- `photos`: `photo_key(thumbnail, camera prior)` → `{paths, verdict, photo}`; triage v2's answer for one picture.
- `reader`: `blob_key(file bytes)` → `{path, outcome}`; the document reader's transcription (scans, screenshots).
- `meta`: the model and provider, versions, the recording's date and its cost.
Paths are informational (the fixture's own, synthetic).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from src.domain.drive_enrichment import (
    DriveProfile, HintItem, KeyValue, PhotoDescription, PhotoObject, ProfileDate, ProfileEntity,
)
from src.domain.drive_enrichment_calls import ProfileRequest
from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus
from src.domain.drive_index import DocumentBrief

SNAPSHOT_FORMAT = 1


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def blob_key(data: bytes) -> str:
    return _sha(data)


def hint_key(chunk_text: str) -> str:
    return _sha(chunk_text.encode("utf-8"))


def photo_key(thumbnail: bytes, camera_prior: bool) -> str:
    return f"{_sha(thumbnail)}:{int(camera_prior)}"


def profile_key(request: ProfileRequest) -> str:
    """Everything the profiler's answer depends on: kind, name, the input and the previous labels."""
    previous = None
    if request.previous is not None:
        previous = [request.previous.doc_type, list(request.previous.life_domains),
                    request.previous.valid_until.isoformat() if request.previous.valid_until else None]
    payload = [request.kind, request.name, request.input.head, request.input.tail, list(request.input.headings),
               list(request.input.child_names), previous]
    return _sha(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))


# -- (de)serialisation of the port messages ---------------------------------------------------------------------------

def profile_to_json(profile: DriveProfile) -> Dict[str, Any]:
    return json.loads(json.dumps(asdict(profile), ensure_ascii=False, default=_default))


def _default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"not serialisable: {type(value).__name__}")


def profile_from_json(data: Mapping[str, Any]) -> DriveProfile:
    return DriveProfile(
        title=data.get("title", ""), what_it_is=data.get("what_it_is", ""), language=data.get("language", ""),
        doc_type=data.get("doc_type", ""), life_domains=tuple(data.get("life_domains", ())),
        dates=tuple(ProfileDate(label=d["label"], iso=date.fromisoformat(d["iso"])) for d in data.get("dates", ())),
        entities=tuple(ProfileEntity(name=e["name"], type=e["type"], printed=e["printed"],
                                     forms=tuple(e.get("forms", ()))) for e in data.get("entities", ())),
        key_values=tuple(KeyValue(label=k["label"], printed=k["printed"], canonical=k["canonical"])
                         for k in data.get("key_values", ())),
        topics=tuple(data.get("topics", ())), topics_native=tuple(data.get("topics_native", ())),
        topic_words=tuple(data.get("topic_words", ())), label_words=tuple(data.get("label_words", ())),
        domain_words=tuple(data.get("domain_words", ())), validity_words=tuple(data.get("validity_words", ())))


def items_to_json(items: Sequence[HintItem]) -> list:
    return [asdict(i) for i in items]


def items_from_json(data: Sequence[Mapping[str, Any]]) -> tuple:
    return tuple(HintItem(text=i["text"], type=i["type"], evidence=i.get("evidence", ""),
                          evidence_start=int(i.get("evidence_start", -1)), evidence_end=int(i.get("evidence_end", -1)))
                 for i in data)


def photo_to_json(photo: Optional[PhotoDescription]) -> Optional[Dict[str, Any]]:
    return None if photo is None else asdict(photo)


def photo_from_json(data: Optional[Mapping[str, Any]]) -> Optional[PhotoDescription]:
    if data is None:
        return None
    return PhotoDescription(description=data.get("description", ""),
                            objects=tuple(PhotoObject(name=o["name"], forms=tuple(o.get("forms", ())))
                                          for o in data.get("objects", ())),
                            scene=data.get("scene", ""), visible_text=data.get("visible_text", ""),
                            scene_labels=tuple(data.get("scene_labels", ())))


def outcome_to_json(outcome: ExtractionOutcome) -> Dict[str, Any]:
    brief = None
    if outcome.brief is not None:
        brief = {"doc_type": outcome.brief.doc_type, "description": outcome.brief.description,
                 "doc_date": outcome.brief.doc_date.isoformat() if outcome.brief.doc_date else None,
                 "parties": list(outcome.brief.parties), "amount": outcome.brief.amount}
    return {"status": outcome.status.value, "text": outcome.text, "pages_total": outcome.pages_total,
            "pages_sent": outcome.pages_sent, "truncated": outcome.truncated, "brief": brief,
            "extracted_by": outcome.extracted_by, "pages_refused": outcome.pages_refused,
            "model_calls": outcome.model_calls, "model_cost": outcome.model_cost,
            "script_rendered": outcome.script_rendered}


def outcome_from_json(data: Mapping[str, Any]) -> ExtractionOutcome:
    """The recorded outcome, as the reader returned it, with no cost: a replay bills nothing."""
    brief = None
    if data.get("brief") is not None:
        b = data["brief"]
        brief = DocumentBrief(doc_type=b.get("doc_type", ""), description=b.get("description", ""),
                              doc_date=datetime.fromisoformat(b["doc_date"]) if b.get("doc_date") else None,
                              parties=tuple(b.get("parties", ())), amount=b.get("amount", ""))
    return ExtractionOutcome(status=ExtractionStatus(data["status"]), text=data.get("text", ""),
                             pages_total=int(data.get("pages_total", 0)), pages_sent=int(data.get("pages_sent", 0)),
                             truncated=bool(data.get("truncated", False)), brief=brief,
                             extracted_by=data.get("extracted_by", ""), pages_refused=int(data.get("pages_refused", 0)),
                             model_calls=0, model_cost=0.0, script_rendered=bool(data.get("script_rendered", False)))


@dataclass
class EnrichmentSnapshot:
    meta: Dict[str, Any] = field(default_factory=dict)
    profiles: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    hints: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    photos: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    reader: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "EnrichmentSnapshot":
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("format") != SNAPSHOT_FORMAT:
            raise ValueError(f"{path}: snapshot format {data.get('format')!r}, expected {SNAPSHOT_FORMAT}")
        return cls(meta=dict(data.get("meta", {})), profiles=dict(data.get("profiles", {})),
                   hints=dict(data.get("hints", {})), photos=dict(data.get("photos", {})),
                   reader=dict(data.get("reader", {})))

    def to_json(self) -> Dict[str, Any]:
        """Sorted maps: a re-record of the same answers is the same file."""
        return {"format": SNAPSHOT_FORMAT, "meta": self.meta,
                "profiles": dict(sorted(self.profiles.items())), "hints": dict(sorted(self.hints.items())),
                "photos": dict(sorted(self.photos.items())), "reader": dict(sorted(self.reader.items()))}

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_json(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    def sha256(self) -> str:
        return _sha(json.dumps(self.to_json(), ensure_ascii=False, sort_keys=True).encode("utf-8"))
