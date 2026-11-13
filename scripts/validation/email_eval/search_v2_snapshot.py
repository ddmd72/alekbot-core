"""
Email search v2 snapshot evaluation (email index v2 RFC §8.5; search slice 4).

Runs the real `EmailCardSearchService` on the in-memory card index over a carded snapshot of the owner's mail and
compares, per real query, what v2 shows with what v1 returned live when the owner asked (the export's `returned_ids`):
the owner's labels say which of those were right. Offline except the query embeddings (`--max-usd` stops the run).

    venv/bin/python -m scripts.validation.email_eval.search_v2_snapshot select --labels L --export X --sample N \
        --seed S --out IDS
    venv/bin/python -m scripts.validation.email_eval.search_v2_snapshot run --cards C --labels L --calls K \
        --zone Z --out R [--slots S] [--prev P] [--max-usd 0.20] [--people-from-firestore]

Tracked and public: no path, id, zone or address is written here; everything private is a flag or comes from `.env`
(`DEV_USER_ID`, `DEV_ACCOUNT_ID`, `GEMINI_API_KEY`). Every output path must be under `scripts/memory/` (gitignored).
Stdout holds counts, item ids and the embedding spend only, never a request or a mail text.

`select`: the ids the snapshot must card: every right id of the answered, non-skipped items, their whole template
series (the v1 records whose `template_key` equals a right id's: a receipt and the receipts that fold with it), and N
random other records (seeded). One id per line.

`run`: the cards JSONL is the slice 3 subscription script's output, one `{"email_id", "document", "vector"}` per line
(`document: null`: a stored mail with no card yet). Per answered, non-skipped item: Today is the date, in `--zone`,
of the earliest call of the item's trace; slots come from `--slots` (item id -> the model's raw slots) or none.
`wrong_shown` counts the shown mails the owner marked not right (a shown mail nobody labelled is unknown, not wrong).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
from datetime import date, datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from src.adapters.in_memory_email_card_index import InMemoryEmailCardIndex
from src.domain.billing import ledger_scope
from src.domain.email_card import EmailCard
from src.domain.email_card_keys import template_key
from src.domain.email_search_query import MailSearchRequest, parse_query_slots
from src.domain.email_search_render import MailSearchResult
from src.ports.confirmed_people_repository import ConfirmedPeopleRepository
from src.ports.embedding_service import EmbeddingService
from src.services.email_card_search_service import EmailCardSearchService

_REPO = Path(__file__).resolve().parents[3]
MEMORY_ROOT = _REPO / "scripts" / "memory"
EXIT_OK, EXIT_SETUP = 0, 2
EMBEDDING_USD_PER_TOKEN = 0.20 / 1_000_000       # billing.py: gemini-embedding-2, input tokens only
CHARS_PER_TOKEN = 3
TOP_RANKED = 50
VERDICTS = ("better", "same", "worse", "needs_id", "no_today")

FromDocument = Callable[[Mapping[str, Any]], EmailCard]


class MeteredEmbeddings(EmbeddingService):
    """An embedding service that adds up what its calls cost (the ledger the adapter writes to) and projects the
    cost of the next text, so the run can stop before it passes `--max-usd`."""

    def __init__(self, inner: EmbeddingService) -> None:
        self._inner = inner
        self.spent = 0.0

    @staticmethod
    def projected(text: str) -> float:
        return len(text) / CHARS_PER_TOKEN * EMBEDDING_USD_PER_TOKEN

    async def get_embedding(self, text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> List[float]:
        with ledger_scope(None) as ledger:
            vector = await self._inner.get_embedding(text, task_type)
        self.spent += ledger.cost()
        return vector

    async def get_embeddings_batch(self, texts: List[str], task_type: str = "RETRIEVAL_DOCUMENT") -> List[List[float]]:
        return [await self.get_embedding(t, task_type) for t in texts]


# -- paths ----------------------------------------------------------------------------------------------------------

def checked_out_path(path: Path, root: Optional[Path] = None) -> Path:
    """`path` resolved, when it lies under `root` (default `scripts/memory/`); SystemExit otherwise: nothing private
    is written to a tracked directory."""
    base = (MEMORY_ROOT if root is None else root).resolve()
    resolved = path.resolve()
    if base not in resolved.parents:
        raise SystemExit("refused: every output path must be under scripts/memory/ (gitignored)")
    return resolved


# -- the labels, the v1 records, the calls --------------------------------------------------------------------------

def answered_items(labels: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    """The owner's labelled items that count: answered and not skipped."""
    return [it for it in labels if it.get("answered") and not it.get("skip")]


def record_template(record: Mapping[str, Any]) -> str:
    """The template key of a v1 record: the domain of its sender address and its subject."""
    address = parseaddr(str(record.get("from_address", "")))[1]
    domain = address.rsplit("@", 1)[-1] if "@" in address else ""
    return template_key(domain, str(record.get("subject", "")))


def select_ids(labels: Sequence[Mapping[str, Any]], records: Sequence[Mapping[str, Any]], sample: int,
               seed: int) -> List[str]:
    """The right ids, their whole template series, then `sample` seeded random other records; each id once."""
    right = list(dict.fromkeys(i for it in answered_items(labels) for i in it.get("right_ids", [])))
    by_id = {str(r["email_id"]): r for r in records}
    templates = {record_template(by_id[i]) for i in right if i in by_id}
    series = [str(r["email_id"]) for r in records if record_template(r) in templates]
    chosen = list(dict.fromkeys([*right, *series]))
    others = sorted(set(by_id) - set(chosen))
    picked = random.Random(seed).sample(others, min(max(sample, 0), len(others)))
    return list(dict.fromkeys([*chosen, *picked]))


def today_of(calls: Sequence[Mapping[str, Any]], trace_id: str, zone: str) -> Optional[date]:
    """The date, in `zone`, of the earliest call of the trace (a naive timestamp is UTC); None without a call."""
    stamps = [_parse_stamp(str(c["timestamp"])) for c in calls if c.get("trace_id") == trace_id and c.get("timestamp")]
    return min(stamps).astimezone(ZoneInfo(zone)).date() if stamps else None


def _parse_stamp(raw: str) -> datetime:
    stamp = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=timezone.utc)


# -- the verdict ----------------------------------------------------------------------------------------------------

def item_metrics(item: Mapping[str, Any], result: MailSearchResult) -> Dict[str, Any]:
    """What v2 and v1 each showed of the owner's labels, and the verdict (module docstring)."""
    right, wrong = set(item.get("right_ids", [])), set(item.get("not_right_ids", []))
    v1 = list(item.get("returned_ids", []))
    shown = set(result.shown_ids)
    folded = {i for ids in result.folded_ids.values() for i in ids}
    ranked = list(result.ranked_ids)
    rank = [n for n, i in enumerate(ranked, 1) if i in right]
    metrics: Dict[str, Any] = {
        "right_shown": len(right & shown), "right_folded": len(right & folded),
        "right_ranked": len(right & set(ranked)), "first_right_rank": rank[0] if rank else None,
        "wrong_shown": len(wrong & shown), "v1_right_shown": len(right & set(v1)),
        "v1_wrong_shown": len(wrong & set(v1))}
    metrics["verdict"] = verdict_of(item, result, metrics)
    return metrics


def verdict_of(item: Mapping[str, Any], result: MailSearchResult, metrics: Mapping[str, Any]) -> str:
    """`better` / `same` / `worse` against v1; `needs_id` for an item with free text and no right id."""
    v1 = list(item.get("returned_ids", []))
    if item.get("none"):
        if result.mode == "nothing":
            return "better" if v1 else "same"
        return "worse" if result.shown_ids else "same"
    if item.get("free_text") and not item.get("right_ids"):
        return "needs_id"
    mine = (metrics["right_shown"] + metrics["right_folded"], -metrics["wrong_shown"])
    theirs = (metrics["v1_right_shown"], -metrics["v1_wrong_shown"])
    return "better" if mine > theirs else "worse" if mine < theirs else "same"


def changed_since(report: Mapping[str, Any], prev: Mapping[str, Any]) -> List[str]:
    """The item ids whose shown ids or verdict differ from the previous report (an id new to this report counts)."""
    before = {i["item_id"]: (i["shown_ids"], i["verdict"]) for i in prev.get("items", [])}
    return [i["item_id"] for i in report["items"] if before.get(i["item_id"]) != (i["shown_ids"], i["verdict"])]


# -- the run --------------------------------------------------------------------------------------------------------

def load_cards(path: Path, from_document: FromDocument) -> Tuple[List[Tuple[EmailCard, Optional[List[float]]]], List[str]]:
    """The cards JSONL: `(card, vector)` per carded line, the ids of the lines with `document: null`."""
    cards: List[Tuple[EmailCard, Optional[List[float]]]] = []
    uncarded: List[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("document") is None:
            uncarded.append(str(row["email_id"]))
        else:
            cards.append((from_document(row["document"]), row.get("vector")))
    return cards, uncarded


async def run_report(*, cards: Sequence[Tuple[EmailCard, Optional[Sequence[float]]]], uncarded: Sequence[str],
                     labels: Sequence[Mapping[str, Any]], calls: Sequence[Mapping[str, Any]], zone: str,
                     slots: Mapping[str, Any], user_id: str, account_id: str, embeddings: EmbeddingService,
                     people: ConfirmedPeopleRepository, max_usd: float) -> Dict[str, Any]:
    """The report: one record per answered item, the verdict counts and the embedding spend."""
    index = InMemoryEmailCardIndex()
    for card, vector in cards:
        index.add(user_id, card, vector)
    for email_id in uncarded:
        index.add_uncarded(user_id, email_id)
    meter = MeteredEmbeddings(embeddings)
    service = EmailCardSearchService(index, meter, people)
    items: List[Dict[str, Any]] = []
    stopped_at: Optional[str] = None
    for item in answered_items(labels):
        item_id, request = str(item["item_id"]), str(item["request"])
        today = today_of(calls, str(item.get("trace_id", "")), zone)
        if today is None:
            items.append({"item_id": item_id, "verdict": "no_today", "shown_ids": []})
            continue
        if meter.spent + meter.projected(request) > max_usd:
            stopped_at = item_id
            break
        raw = slots.get(item_id)
        parsed = parse_query_slots(raw, request) if raw is not None else None
        result = await service.search(user_id, account_id, MailSearchRequest(request, today, parsed))
        items.append({
            "item_id": item_id, "request": request, "today": today.isoformat(), "mode": result.mode,
            "shown_ids": list(result.shown_ids), "folded_ids": {k: list(v) for k, v in result.folded_ids.items()},
            "ranked_ids": list(result.ranked_ids[:TOP_RANKED]), "right_ids": list(item.get("right_ids", [])),
            "not_right_ids": list(item.get("not_right_ids", [])), "none": bool(item.get("none")),
            "v1_returned_ids": list(item.get("returned_ids", [])), **item_metrics(item, result)})
    return {"items": items, "stopped_at": stopped_at, "embedding_usd": round(meter.spent, 6),
            "verdicts": {v: sum(1 for i in items if i["verdict"] == v) for v in VERDICTS},
            "modes": {m: sum(1 for i in items if i.get("mode") == m)
                      for m in sorted({str(i["mode"]) for i in items if "mode" in i})}}


# -- command line ----------------------------------------------------------------------------------------------------

def _load_json(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _default_from_document() -> FromDocument:
    """The slice 3 reader of a stored card document (imported late: it lands with that slice)."""
    try:
        from src.domain.email_card_document import card_from_document
    except ImportError as exc:
        raise SystemExit("src.domain.email_card_document.card_from_document is missing: merge the slice 3 branch "
                         "(feat/email-card-writer) first") from exc
    return card_from_document  # type: ignore[no-any-return]


def _select(args: argparse.Namespace) -> int:
    out = checked_out_path(Path(args.out))
    ids = select_ids(_load_json(args.labels), _load_json(args.export), args.sample, args.seed)
    out.write_text("\n".join(ids) + "\n", encoding="utf-8")
    print(f"selected {len(ids)} ids")
    return EXIT_OK


async def _run(args: argparse.Namespace) -> int:
    out = checked_out_path(Path(args.out))
    from dotenv import load_dotenv
    load_dotenv(_REPO / ".env")
    user_id, account_id = os.getenv("DEV_USER_ID", ""), os.getenv("DEV_ACCOUNT_ID", "")
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not (user_id and account_id and api_key):
        print("DEV_USER_ID, DEV_ACCOUNT_ID and GEMINI_API_KEY must be set in .env", file=sys.stderr)
        return EXIT_SETUP
    from src.adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter
    cards, uncarded = load_cards(Path(args.cards), _default_from_document())
    people: ConfirmedPeopleRepository
    if args.people_from_firestore:
        from scripts._firestore import async_client
        from src.adapters.firestore_confirmed_people_repo import FirestoreConfirmedPeopleRepository
        from src.config.environment import EnvironmentConfig
        people = FirestoreConfirmedPeopleRepository(async_client(), EnvironmentConfig())
    else:
        people = _NoPeople()
    report = await run_report(
        cards=cards, uncarded=uncarded, labels=_load_json(args.labels), calls=_load_json(args.calls), zone=args.zone,
        slots=_load_json(args.slots) if args.slots else {}, user_id=user_id, account_id=account_id,
        embeddings=GeminiEmbeddingAdapter(api_key=api_key), people=people, max_usd=args.max_usd)
    changed = changed_since(report, _load_json(args.prev)) if args.prev else []
    report["changed_since_prev"] = changed
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"verdicts {report['verdicts']}\nmodes {report['modes']}\nembedding_usd {report['embedding_usd']}")
    if report["stopped_at"]:
        print(f"stopped before item {report['stopped_at']} (--max-usd)")
    if args.prev:
        print(f"changed since prev: {changed}")
    return EXIT_OK


class _NoPeople(ConfirmedPeopleRepository):
    """No confirmed list (the default: nothing read from Firestore)."""

    async def get(self, account_id: str) -> None:
        return None

    async def replace(self, account_id: str, people: Any, source: str) -> None:
        raise NotImplementedError("the snapshot harness never writes the list")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Email search v2 snapshot evaluation (module docstring).")
    sub = parser.add_subparsers(dest="command", required=True)
    pick = sub.add_parser("select", help="the ids the snapshot must card")
    pick.add_argument("--labels", required=True)
    pick.add_argument("--export", required=True)
    pick.add_argument("--sample", type=int, default=0)
    pick.add_argument("--seed", type=int, default=0)
    pick.add_argument("--out", required=True)
    go = sub.add_parser("run", help="run the labelled queries on the carded snapshot")
    go.add_argument("--cards", required=True)
    go.add_argument("--labels", required=True)
    go.add_argument("--calls", required=True)
    go.add_argument("--zone", required=True)
    go.add_argument("--out", required=True)
    go.add_argument("--slots")
    go.add_argument("--prev")
    go.add_argument("--max-usd", type=float, default=0.20)
    go.add_argument("--people-from-firestore", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "select":
        return _select(args)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
