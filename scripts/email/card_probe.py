#!/usr/bin/env python3
"""Paid dry run of the email card writer, the production path (email index v2, slice 3). Writes nothing: no Firestore
write, no file; it prints each card's fields and the cost.

    python scripts/email/card_probe.py --ids IDS_FILE [--max-usd 0.50] [--embed] [--concurrency 4]

Runs `EmailCardService.card` per id with the pinned model (`EMAIL_CARD_MODEL`, default Flash-Lite) through the real prompt
assembly, so the two tokens, the blueprint and the profile of `prompts_snapshot/` must be uploaded first. The spend cap is
enforced before each call: spent + projected (input characters / 3 tokens, 900 output tokens) must stay under `--max-usd`.
Watch: valid JSON on every mail, the mean cost per carded mail, and what the thought tokens add (the call reports its cost).
Ids come from the file, the dev user from `.env` (`DEV_USER_ID`, `DEV_ACCOUNT_ID`)."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.domain.billing import calculate_cost  # noqa: E402
from src.domain.email_card import CardState  # noqa: E402
from src.domain.email_card_prompt import render_card_static_block  # noqa: E402
from src.domain.email_card_text import card_embedding_text, render_result_line  # noqa: E402
from src.domain.email_card_reply import CardWriterReply  # noqa: E402

PROJECTED_OUTPUT_TOKENS = 900


def projected_usd(model: str, input_chars: int) -> float:
    """What one call is expected to cost: the input at 3 characters a token plus a typical answer."""
    return calculate_cost(model, input_chars // 3, PROJECTED_OUTPUT_TOKENS)


class _SpendCap:
    """Reserve before a call, settle after it: spent + reserved + the next projection never passes the cap."""

    def __init__(self, cap: float) -> None:
        self.cap, self.spent, self.reserved = cap, 0.0, 0.0
        self._lock = asyncio.Lock()

    async def reserve(self, projected: float) -> bool:
        async with self._lock:
            if self.spent + self.reserved + projected > self.cap:
                return False
            self.reserved += projected
            return True

    async def settle(self, projected: float, actual: float) -> None:
        async with self._lock:
            self.reserved -= projected
            self.spent += actual


class _CappedWriter:
    """The real writer behind the cap: the service calls `write(account_id, static_block, text)`."""

    def __init__(self, inner: Any, model: str, cap: _SpendCap) -> None:
        self._inner, self._model, self._cap = inner, model, cap
        self.refused_by_cap = 0

    async def write(self, account_id: str, static_block: str, card_input_text: str) -> CardWriterReply:
        projected = projected_usd(self._model, len(static_block) + len(card_input_text) + 12_000)
        if not await self._cap.reserve(projected):
            self.refused_by_cap += 1
            return CardWriterReply.unavailable(self._model)
        reply = await self._inner.write(account_id, static_block, card_input_text)
        await self._cap.settle(projected, reply.usd)
        return reply


async def run(args: argparse.Namespace) -> int:
    from scripts.email.card_script_support import fetch_source, open_session, read_ids
    from google.cloud import firestore
    from src.composition.email_card_runner import EmailCardRunner, card_writer_model
    from src.composition.service_container import ServiceContainer
    from src.adapters.firestore_account_repo import FirestoreAccountRepository
    from src.config.settings import load_settings
    from src.services.email_card_service import EmailCardService
    from src.services.prompt_builder import PromptBuilder

    ids = read_ids(Path(args.ids))
    session = await open_session()
    config = load_settings()
    env_config = config["ENVIRONMENT_CONFIG"]
    db = firestore.AsyncClient(database=os.getenv("FIRESTORE_DATABASE", "us-production"))
    accounts = FirestoreAccountRepository(db_client=db, collection_name=env_config.account_collection_name)
    container = ServiceContainer(config=config, db_client=db, env_config=env_config, account_repo=accounts)
    model = card_writer_model(os.getenv("EMAIL_CARD_MODEL"))
    runner = EmailCardRunner(container.context_builder,
                             PromptBuilder(repo=None, assembly_service=container.assembly_service), model)
    cap = _SpendCap(args.max_usd)
    writer = _CappedWriter(runner, model, cap)
    embeddings = container.embedding_service if args.embed else _NoEmbeddings()
    service = EmailCardService(session.gmail, session.attachments, writer, embeddings)  # type: ignore[arg-type]
    static = render_card_static_block(session.people, session.mailbox)
    sem = asyncio.Semaphore(args.concurrency)
    states: "Counter[str]" = Counter()
    carded_usd: List[float] = []
    started = time.time()

    async def one(email_id: str) -> None:
        async with sem:
            source, why = await fetch_source(session, email_id)
            if source is None:
                states[why] += 1
                print(f"{email_id} fetch={why}")
                return
            result = await service.card(
                session.credential, session.account_id, source, zone=session.zone,
                owner_addresses=session.owner_addresses, static_block=static, people=session.people,
                deadline_at=time.time() + 600, cache={})
            states[f"{result.state.value}:{result.reason or '-'}"] += 1
            reply = result.reply
            print(f"{email_id} state={result.state.value} reason={result.reason or '-'} "
                  f"model={reply.model if reply else '-'} calls={reply.calls if reply else 0} "
                  f"usd={reply.usd if reply else 0.0:.5f}")
            if reply is not None and result.state is CardState.CARDED:
                carded_usd.append(reply.usd)
            card = result.card
            if card is None:
                return
            print(f"  {render_result_line(card)}")
            print(f"  about={card.about.value} people={[(p.printed, p.linked_by) for p in card.people]}")
            print(f"  times={[(t.label, t.start.isoformat(), t.end.isoformat(), t.verified) for t in card.times]}")
            print(f"  key values={[(g.field, g.value, g.grade) for g in card.profile.graded]}")
            print(f"  card_embedding_text chars={len(card_embedding_text(card))} vector={'yes' if result.vector else 'no'}")

    await asyncio.gather(*(one(i) for i in ids))
    mean = sum(carded_usd) / len(carded_usd) if carded_usd else 0.0
    print(f"mails={len(ids)} wall={time.time() - started:.0f}s spent=${cap.spent:.4f} (cap ${args.max_usd}) "
          f"stopped_by_cap={writer.refused_by_cap} mean_usd_per_carded={mean:.5f}")
    print("outcomes:", dict(states))
    return 0


class _NoEmbeddings:
    async def get_embedding(self, *_: Any) -> List[float]:
        raise RuntimeError("embedding needs --embed")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ids", required=True, help="file with one Gmail id per line")
    ap.add_argument("--max-usd", type=float, default=0.50)
    ap.add_argument("--embed", action="store_true")
    ap.add_argument("--concurrency", type=int, default=4)
    return asyncio.run(run(ap.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
