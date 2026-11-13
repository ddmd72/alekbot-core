#!/usr/bin/env python3
"""The one-off re-card of the mailbox on the owner's subscription (email index v2, slice 3; card D9, D10).

Cards are written by `claude -p --model sonnet`, three mails per call, then built and keyed by the same code as the
online path (`EmailCardService.prepare` / `.finish` / `.embed`): the same input builder, parse, build and keys. Nothing is
written to Firestore, GCS or any store: each card becomes one JSON line in a local file under `scripts/memory/`
(gitignored), read back by slice 4 with `card_from_document`. The Gemini embedding API is used only with `--embed`.

    python scripts/email/recard_subscription.py --ids IDS_FILE --out scripts/memory/email_cards/run.jsonl
        [--limit N] [--concurrency 30] [--thinking on|off] [--embed --max-usd 1.00]

`--thinking on` (the default) keeps Claude's extended thinking: measured, switching it off was 2.7x faster but invented
content rose from 4 to 11 of 27 cards. `off` sets MAX_THINKING_TOKENS=0 on the child. A call whose answer is not valid
JSON, or lacks a mail's card, is retried once; the mails still missing are logged as failed, never guessed (re-run them
with `--ids`). Ids already carded in `--out` are skipped. Stdout carries counts only, never mail text.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import re
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.domain.billing import calculate_cost  # noqa: E402
from src.domain.email_card import CardState  # noqa: E402
from src.domain.email_card_answer import parse_mail_answer  # noqa: E402
from src.domain.email_card_document import card_document  # noqa: E402
from src.domain.email_card_prompt import (  # noqa: E402
    card_response_schema, card_task_line, render_card_prompt, render_card_static_block,
)
from src.domain.exceptions import AttachmentParserUnavailable, EmailMessageGone, EmailPartUnavailable  # noqa: E402
from src.domain.untrusted_text import EXTERNAL_REMINDER  # noqa: E402

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
X3_DIR = HERE / "card_prompts_x3"
SNAPSHOT_TOKENS = REPO / "prompts_snapshot" / "tokens" / "system"
PATH_X3 = "subscription-sonnet-x3"
PATH_X1 = "subscription-sonnet-x1"
GROUP_SIZE = 3
CHUNK = 60
GMAIL_CONCURRENCY = 5
CALL_TIMEOUT_S = 900
EMBED_MODEL = "gemini-embedding-2"
_SINGLE_SENTENCE = "Write the card of this mail as the output format says."
_TRIPLE_SENTENCE = "Write one card for each of the three mails above, in their order, as the output format says."
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")

# A call: (system, user) -> the model's answer text, or None when the call itself failed.
Call = Callable[[str, str], Awaitable[Optional[str]]]


# -- pure helpers (tested in tests/unit/scripts/test_recard_subscription_helpers.py) ----------------------------------

def strip_fences(text: str) -> str:
    """The text without a markdown code fence around it (with or without the `json` tag)."""
    return _FENCE.sub("", text.strip())


def triples(ids: Sequence[str]) -> List[Tuple[str, ...]]:
    """Groups of three in input order; the final one or two mails each get a group of their own (one per call)."""
    full = len(ids) - len(ids) % GROUP_SIZE
    groups: List[Tuple[str, ...]] = [tuple(ids[i:i + GROUP_SIZE]) for i in range(0, full, GROUP_SIZE)]
    groups.extend((i,) for i in ids[full:])
    return groups


def x3_user_message(mails: Sequence[Tuple[str, str]]) -> str:
    """The user message of a three-mail call: the external-content reminder once, each mail's block under its own
    `<mail id=...>` wrapper in order, then the task line for three mails. `mails` are (email_id, `CardInput.text`)."""
    lead = f"{EXTERNAL_REMINDER}\n\n"
    blocks = []
    for email_id, text in mails:
        if not text.startswith(lead):
            raise ValueError("a card input must start with the external-content reminder")
        blocks.append(f'<mail id="{email_id}">\n{text[len(lead):]}\n</mail>')
    task = card_task_line()
    if not task.startswith(_SINGLE_SENTENCE):
        raise ValueError("the task line no longer starts with the single-mail sentence")
    task = _TRIPLE_SENTENCE + task[len(_SINGLE_SENTENCE):]
    return EXTERNAL_REMINDER + "\n\n" + "\n\n".join(blocks) + "\n\n" + task


def x3_schema() -> Dict[str, Any]:
    """The schema text of a three-mail call: `{"cards": [card + email_id]}`; the subscription only reads it as text."""
    item = copy.deepcopy(card_response_schema())
    item["properties"] = {"email_id": {"type": "string", "description": "The id of the mail wrapper this card describes."},
                          **item["properties"]}
    item["required"] = ["email_id", *item["required"]]
    return {"type": "object", "properties": {"cards": {"type": "array", "description": "One card per mail, in input order.",
                                                        "items": item}}, "required": ["cards"]}


def cards_by_id(answer: Any) -> Dict[str, Dict[str, Any]]:
    """The cards of a parsed three-mail answer keyed by their `email_id` (never by position); an entry without a string
    id and anything that is not a card list are ignored."""
    if not isinstance(answer, dict) or not isinstance(answer.get("cards"), list):
        return {}
    return {c["email_id"]: c for c in answer["cards"] if isinstance(c, dict) and isinstance(c.get("email_id"), str)}


def missing_ids(expected: Sequence[str], found: Dict[str, Any]) -> List[str]:
    return [i for i in expected if i not in found]


def _token_body(path: Path) -> str:
    """The body of a snapshot token file: after the closing `---` of its frontmatter."""
    text = path.read_text(encoding="utf-8")
    return text.split("\n---\n", 1)[1].strip("\n") if text.startswith("---\n") else text.strip("\n")


def system_text(cognitive: str, output_format: str, static_block: str, schema: Dict[str, Any]) -> str:
    return (render_card_prompt(cognitive) + "\n\n" + render_card_prompt(output_format) + "\n\n" + static_block
            + "\n\nThe JSON must satisfy this schema:\n" + json.dumps(schema))


def x3_system(static_block: str) -> str:
    return system_text((X3_DIR / "COGNITIVE_PROCESS_EMAIL_CARD_X3.txt").read_text(encoding="utf-8").strip("\n"),
                       (X3_DIR / "OUTPUT_FORMAT_EMAIL_CARD_X3.txt").read_text(encoding="utf-8").strip("\n"),
                       static_block, x3_schema())


def x1_system(static_block: str) -> str:
    return system_text(_token_body(SNAPSHOT_TOKENS / "COGNITIVE_PROCESS_EMAIL_CARD.groovy"),
                       _token_body(SNAPSHOT_TOKENS / "OUTPUT_FORMAT_EMAIL_CARD.groovy"), static_block,
                       card_response_schema())


def child_env(thinking: str) -> Dict[str, str]:
    """The child's environment: the local proxy needs NO_PROXY; `off` switches extended thinking off."""
    env = {**os.environ, "NO_PROXY": "*", "no_proxy": "*"}
    if thinking == "off":
        env["MAX_THINKING_TOKENS"] = "0"
    else:
        env.pop("MAX_THINKING_TOKENS", None)
    return env


def claude_command(system: str) -> List[str]:
    return ["claude", "-p", "--model", "sonnet", "--output-format", "json", "--tools", "", "--no-session-persistence",
            "--disable-slash-commands", "--strict-mcp-config", "--system-prompt", system]


def answer_text(stdout: str) -> Optional[str]:
    """The `result` text of `claude -p --output-format json`, None when the call reports an error or is unreadable."""
    try:
        out = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(out, dict) or out.get("is_error"):
        return None
    result = out.get("result")
    return result if isinstance(result, str) else None


def make_call(thinking: str, cwd: str, sem: asyncio.Semaphore, timings: List[float]) -> Call:
    env = child_env(thinking)

    async def call(system: str, user: str) -> Optional[str]:
        async with sem:
            started = time.time()
            proc = await asyncio.create_subprocess_exec(
                *claude_command(system), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, cwd=cwd, env=env)
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(user.encode("utf-8")), CALL_TIMEOUT_S)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return None
            finally:
                timings.append(time.time() - started)
            return answer_text(stdout.decode("utf-8", "replace")) if proc.returncode == 0 else None

    return call


# -- one group ---------------------------------------------------------------------------------------------------------

def parse_cards(text: Optional[str], ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """The valid cards of one answer by mail id; a card `parse_mail_answer` rejects counts as missing."""
    if text is None:
        return {}
    try:
        data = json.loads(strip_fences(text))
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    found = cards_by_id(data) if len(ids) > 1 or "cards" in data else {ids[0]: data}
    ok: Dict[str, Dict[str, Any]] = {}
    for email_id in ids:
        card = found.get(email_id)
        if card is None:
            continue
        try:
            parse_mail_answer(json.dumps(card))
        except ValueError:
            continue
        ok[email_id] = card
    return ok


async def write_group(call: Call, system_x3: str, system_x1: str, group: Sequence[Tuple[str, str]]) -> Dict[str, Dict[str, Any]]:
    """The cards of one group of (email_id, input text): one call (three mails) or one single-mail call; a call that
    leaves a mail without a valid card is retried once as a whole; the rest stay missing."""
    ids = [i for i, _ in group]
    if len(group) == GROUP_SIZE:
        system, user = system_x3, x3_user_message(group)
    else:
        system, user = system_x1, f"{group[0][1]}\n\n{card_task_line()}"
    got = parse_cards(await call(system, user), ids)
    if missing_ids(ids, got):
        for email_id, card in parse_cards(await call(system, user), ids).items():
            got.setdefault(email_id, card)
    return got


# -- the run -----------------------------------------------------------------------------------------------------------

@dataclass
class Totals:
    states: "Counter[str]"
    reasons: "Counter[str]"
    embed_usd: float = 0.0


def done_ids(out: Path) -> set:
    """Ids that already have a card in `out` (a failure line is not done)."""
    if not out.exists():
        return set()
    done = set()
    for line in out.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("document") is not None:
            done.add(row["email_id"])
    return done


def under_scripts_memory(path: Path) -> bool:
    try:
        path.resolve().relative_to((REPO / "scripts" / "memory").resolve())
    except ValueError:
        return False
    return True


async def run(args: argparse.Namespace) -> int:
    from scripts.email.card_script_support import fetch_source, open_session, read_ids
    from src.services.email_card_service import EmailCardService, PreparedMail  # noqa: E402

    out = Path(args.out)
    if not under_scripts_memory(out):
        print("--out must be under scripts/memory/", file=sys.stderr)
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    ids = [i for i in read_ids(Path(args.ids), args.limit) if i not in done_ids(out)]
    session = await open_session()
    embedder = None
    if args.embed:
        from src.adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter
        embedder = GeminiEmbeddingAdapter(api_key=os.environ["GEMINI_API_KEY"])
    service = EmailCardService(session.gmail, session.attachments, _NoWriter(), embedder or _NoEmbeddings())  # type: ignore[arg-type]
    static = render_card_static_block(session.people, session.mailbox)
    system_x3, system_x1 = x3_system(static), x1_system(static)
    totals = Totals(Counter(), Counter())
    timings: List[float] = []
    started = time.time()
    gmail_sem = asyncio.Semaphore(GMAIL_CONCURRENCY)
    deadline_at = time.time() + 3600 * 12
    cache: Dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="recard_cwd_") as cwd, out.open("a", encoding="utf-8") as sink:
        call = make_call(args.thinking, cwd, asyncio.Semaphore(args.concurrency), timings)

        def emit(email_id: str, document: Any, vector: Any, path: str, state: str, reason: str) -> None:
            sink.write(json.dumps({"email_id": email_id, "document": document, "vector": vector, "path": path,
                                   "state": state, "reason": reason}, ensure_ascii=False) + "\n")
            sink.flush()
            totals.states[state] += 1
            totals.reasons[reason or "-"] += 1

        async def prepare(email_id: str) -> Optional[PreparedMail]:
            async with gmail_sem:
                source, why = await fetch_source(session, email_id)
                if source is None:
                    emit(email_id, None, None, "", CardState.SOURCE_GONE.value if why == "gone" else CardState.PENDING.value, why)
                    return None
                try:
                    return await service.prepare(session.credential, session.account_id, source, zone=session.zone,
                                                 owner_addresses=session.owner_addresses, deadline_at=deadline_at,
                                                 cache=cache)
                except EmailMessageGone:
                    emit(email_id, None, None, "", CardState.SOURCE_GONE.value, "gone")
                except (AttachmentParserUnavailable, EmailPartUnavailable):
                    emit(email_id, None, None, "", CardState.PENDING.value, "attachments")
                return None

        async def one_group(prepared: Sequence[PreparedMail]) -> None:
            group = [(p.mail.email_id, p.card_input.text) for p in prepared]
            got = await write_group(call, system_x3, system_x1, group)
            path = PATH_X3 if len(prepared) == GROUP_SIZE else PATH_X1
            for p in prepared:
                email_id = p.mail.email_id
                if email_id not in got:
                    emit(email_id, None, None, path, CardState.PENDING.value, "failed")
                    continue
                card = service.finish(p, parse_mail_answer(json.dumps(got[email_id])), session.people)
                vector = None
                if args.embed and card.state is CardState.CARDED and totals.embed_usd < args.max_usd:
                    from src.domain.email_card_text import card_embedding_text
                    totals.embed_usd += calculate_cost(EMBED_MODEL, len(card_embedding_text(card)) // 3, 0)
                    vector = await service.embed(card)
                emit(email_id, card_document(card), vector, path, card.state.value, "")

        for start in range(0, len(ids), CHUNK):
            prepared = [p for p in await asyncio.gather(*(prepare(i) for i in ids[start:start + CHUNK])) if p is not None]
            by_id = {p.mail.email_id: p for p in prepared}
            await asyncio.gather(*(one_group([by_id[i] for i in g]) for g in triples([p.mail.email_id for p in prepared])))
    mean = sum(timings) / len(timings) if timings else 0.0
    print(f"mails={len(ids)} calls={len(timings)} wall={time.time() - started:.0f}s mean_call={mean:.1f}s "
          f"embed_usd={totals.embed_usd:.4f}")
    print("states:", dict(totals.states))
    print("reasons:", dict(totals.reasons))
    failed = totals.reasons.get("failed", 0)
    if failed:
        print(f"{failed} mail(s) failed; re-run them with --ids (resume skips the carded ones)")
    return 0


class _NoWriter:
    async def write(self, *_: Any) -> Any:
        raise RuntimeError("the subscription script never calls the online writer")


class _NoEmbeddings:
    async def get_embedding(self, *_: Any) -> List[float]:
        raise RuntimeError("embedding needs --embed")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ids", required=True, help="file with one Gmail id per line")
    ap.add_argument("--out", required=True, help="JSONL file under scripts/memory/")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=30, help="claude -p calls in flight")
    ap.add_argument("--thinking", choices=("on", "off"), default="on")
    ap.add_argument("--embed", action="store_true")
    ap.add_argument("--max-usd", type=float, default=1.00, help="embedding spend cap")
    args = ap.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
