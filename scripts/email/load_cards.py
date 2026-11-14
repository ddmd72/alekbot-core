"""Load re-carded mail into the live card store (email index v2, slice 7).

Reads the JSONL written by `recard_subscription.py` (one line per mail: email_id, document, vector, path, state, reason;
the LAST line per email_id wins) and writes each settled card (`carded` / `empty`) onto the stored mail document exactly
as the online path does: `settled_fields` + `write_card` with compare-and-set, the vector of a `carded` card from
`card_embedding_text` embedded as RETRIEVAL_DOCUMENT. A mail the store does not hold is never created (`missing`); one that
already carries a card is skipped unless `--overwrite`.

    python scripts/email/load_cards.py --in scripts/memory/email_cards/full.jsonl [--limit N] [--apply]
        [--max-usd 2.00] [--overwrite] [--model-label subscription-sonnet-x3]

Dry run is the default: the store is read, nothing is written, no embedding is called. `--apply` writes and embeds.
Embedding spend is metered from the tokens the adapter reports (a ledger scope around each call; an adapter that reports
none is estimated at len(text)//3 as `recard_subscription.py --embed` does) and priced by `calculate_cost`; new
embeddings stop once spent reaches `--max-usd` (later cards are written with `vector_pending=True`). Prints counts only:
never ids, subjects, card text or vectors. `DEV_USER_ID` / `DEV_ACCOUNT_ID` come from `.env`."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.domain.billing import calculate_cost, ledger_scope  # noqa: E402
from src.domain.confirmed_people import ConfirmedPeople  # noqa: E402
from src.domain.email_card import CardState, EmailCard  # noqa: E402
from src.domain.email_card_document import card_from_document  # noqa: E402
from src.domain.email_card_lifecycle import (  # noqa: E402
    CardExpect, people_digest, settled_fields,
)
from src.domain.email_card_text import card_embedding_text  # noqa: E402
from src.ports.email_card_store import EmailCardStore  # noqa: E402
from src.utils.logger import logger  # noqa: E402

REPO = Path(__file__).resolve().parent.parent.parent
EMBED_MODEL = "gemini-embedding-2"
READ_BATCH = 500
EMBED_CONCURRENCY = 10
REPORT_EVERY = 500
DEFAULT_MODEL_LABEL = "subscription-sonnet-x3"


def under_scripts_memory(path: Path) -> bool:
    try:
        path.resolve().relative_to((REPO / "scripts" / "memory").resolve())
    except ValueError:
        return False
    return True


def latest_rows(path: Path) -> Dict[str, Dict[str, Any]]:
    """The last well-formed line per email_id, in first-seen order of the ids."""
    rows: Dict[str, Dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("email_id"), str):
            rows[row["email_id"]] = row
    return rows


@dataclass
class Meter:
    """Embedding spend: priced from reported tokens, hard-failing when a priced model reports nonzero calls at zero cost."""
    max_usd: float
    tokens: int = 0
    calls: int = 0
    usd: float = 0.0

    @property
    def exhausted(self) -> bool:
        return self.usd >= self.max_usd

    def add(self, tokens: int) -> None:
        self.calls += 1
        self.tokens += tokens
        self.usd += calculate_cost(EMBED_MODEL, tokens, 0)

    def line(self) -> str:
        return f"embed_usd={self.usd:.4f} embed_tokens={self.tokens} embed_calls={self.calls}"

    def check(self) -> None:
        if self.calls and self.usd <= 0.0:
            raise RuntimeError(f"embedding meter bug: {self.calls} calls priced at 0.0000 ({self.line()})")


@dataclass
class Report:
    counts: "Counter[str]" = field(default_factory=Counter)
    meter: Meter = field(default_factory=lambda: Meter(0.0))
    estimate_tokens: int = 0

    def lines(self, apply: bool) -> List[str]:
        out = [f"{k}={v}" for k, v in sorted(self.counts.items())]
        out.append(self.meter.line() if apply else f"dry run: no writes, no embedding; estimate_tokens={self.estimate_tokens}")
        return out


async def _embed(embedder: Any, card: EmailCard, meter: Meter) -> Optional[List[float]]:
    """One vector, metered; None on a failed call (the card is then stored vector-pending, as online)."""
    text = card_embedding_text(card)
    try:
        with ledger_scope(None) as ledger:
            vector = list(await embedder.get_embedding(text, "RETRIEVAL_DOCUMENT"))
    except Exception as exc:
        logger.error(f"[LoadCards] embedding failed: {type(exc).__name__}")
        return None
    reported = sum(leg.prompt_tokens for leg in ledger.by_model.values())
    meter.add(reported if reported > 0 else max(1, len(text) // 3))
    return vector


async def load(rows: Mapping[str, Mapping[str, Any]], store: EmailCardStore, embedder: Any,
               people: Optional[ConfirmedPeople], user_id: str, *, apply: bool, overwrite: bool = False,
               limit: int = 0, max_usd: float = 2.0, model_label: str = DEFAULT_MODEL_LABEL,
               now: Optional[datetime] = None, emit: Any = print) -> Report:
    report = Report(meter=Meter(max_usd))
    counts = report.counts
    settled: List[Tuple[str, EmailCard, Optional[List[float]]]] = []
    for email_id, row in rows.items():
        if row.get("document") is None:
            counts[f"not_carded:{row.get('state') or '-'}:{row.get('reason') or '-'}"] += 1
            continue
        try:
            card = card_from_document(row["document"])
        except ValueError:
            counts["malformed"] += 1
            continue
        if card.state not in (CardState.CARDED, CardState.EMPTY):
            counts[f"not_carded:{card.state.value}:-"] += 1
            continue
        vector = row.get("vector")
        settled.append((email_id, card, list(vector) if vector else None))
    if limit:
        settled = settled[:limit]

    expects: Dict[str, CardExpect] = {}
    for start in range(0, len(settled), READ_BATCH):
        expects.update(await store.read_expect(user_id, [i for i, _, _ in settled[start:start + READ_BATCH]]))

    todo: List[Tuple[str, EmailCard, Optional[List[float]], CardExpect]] = []
    for email_id, card, vector in settled:
        expect = expects.get(email_id)
        if expect is None:
            counts["missing"] += 1
        elif expect.card_version is not None and not overwrite:
            counts["already_carded"] += 1
        else:
            todo.append((email_id, card, vector, expect))
    counts["to_write"] = len(todo)

    if not apply:
        report.estimate_tokens = sum(len(card_embedding_text(c)) // 3 for _, c, v, _ in todo
                                     if c.state is CardState.CARDED and v is None)
        return report

    stamp = now or datetime.now(timezone.utc)
    sem = asyncio.Semaphore(EMBED_CONCURRENCY)
    done = 0

    async def one(email_id: str, card: EmailCard, vector: Optional[List[float]], expect: CardExpect) -> None:
        nonlocal done
        if card.state is CardState.CARDED and vector is None:
            async with sem:
                if not report.meter.exhausted:
                    vector = await _embed(embedder, card, report.meter)
        pending = card.state is CardState.CARDED and vector is None
        fields = settled_fields(card, digest=people_digest(card, people), model=model_label, now=stamp,
                                vector_pending=pending)
        result = await store.write_card(user_id, email_id, fields, vector, expect)
        counts[result.value] += 1
        if pending:
            counts["vector_pending"] += 1
        done += 1
        if done % REPORT_EVERY == 0:
            report.meter.check()
            emit(f"progress={done} {report.meter.line()}")

    await asyncio.gather(*(one(*item) for item in todo))
    report.meter.check()
    return report


async def run(args: argparse.Namespace) -> int:
    path = Path(args.input)
    if not under_scripts_memory(path):
        print("--in must be under scripts/memory/", file=sys.stderr)
        return 2
    user_id, account_id = os.getenv("DEV_USER_ID"), os.getenv("DEV_ACCOUNT_ID")
    if not user_id or not account_id:
        print("DEV_USER_ID / DEV_ACCOUNT_ID are not set (.env)", file=sys.stderr)
        return 2
    from scripts import _firestore
    from src.adapters.firestore_confirmed_people_repo import FirestoreConfirmedPeopleRepository
    from src.adapters.firestore_email_card_store import FirestoreEmailCardStore
    from src.config.environment import EnvironmentConfig

    env_config = EnvironmentConfig()
    db = _firestore.async_client()
    try:
        people = await FirestoreConfirmedPeopleRepository(db, env_config).get(account_id)   # read only
    except ValueError:
        print("confirmed people list unreadable: digests use an empty list", file=sys.stderr)
        people = None
    embedder = None
    if args.apply:
        from src.adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter
        embedder = GeminiEmbeddingAdapter(api_key=os.environ["GEMINI_API_KEY"])
    report = await load(latest_rows(path), FirestoreEmailCardStore(db, env_config), embedder, people, user_id,
                        apply=args.apply, overwrite=args.overwrite, limit=args.limit, max_usd=args.max_usd,
                        model_label=args.model_label)
    for line in report.lines(args.apply):
        print(line)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="input", required=True, help="JSONL file under scripts/memory/")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--apply", action="store_true", help="write and embed (default: dry run)")
    ap.add_argument("--max-usd", type=float, default=2.00, help="embedding spend cap")
    ap.add_argument("--overwrite", action="store_true", help="rewrite a mail that already carries a card")
    ap.add_argument("--model-label", default=DEFAULT_MODEL_LABEL)
    sys.exit(asyncio.run(run(ap.parse_args())))


if __name__ == "__main__":
    main()
