"""
Bring the live prompt tokens in line with RRULE reminders (2026-07-30).

Two tokens taught the pre-RRULE schedule model and would keep the LLM emitting
``{type, interval}`` into a field that is now an RFC 5545 rule string:

  COGNITIVE_PROCESS_NOTES        — parameter_rules.recurrence (the specialist's schema)
  PROTOCOL_SMART_AGENT_SELECTION — notes_agent block (the orchestrator's delegation guide);
                                   it also pointed at a "working_memory pending_notes" block
                                   that is actually rendered as ``active_reminders``.

Every edit is anchored on exact existing text and aborts if an anchor is missing, so a
partially re-worded token is never silently half-patched.

    python scripts/prompt/migrate_reminder_rrule_tokens.py --dry-run
    python scripts/prompt/migrate_reminder_rrule_tokens.py --apply
    python scripts/prompt/migrate_reminder_rrule_tokens.py --revert <backup.json>

Backups land in scripts/memory/ (gitignored — live content).
The prompt cache holds 24h; a change is visible to new sessions after it expires.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from google.cloud import firestore  # noqa: E402

from src.config.environment import EnvironmentConfig  # noqa: E402

_BACKUP_DIR = Path(__file__).resolve().parents[1] / "memory"

# --------------------------------------------------------------------------- #
# COGNITIVE_PROCESS_NOTES                                                      #
# --------------------------------------------------------------------------- #

_NOTES_OLD_RECURRENCE = '''        recurrence: """
            Default: type='once' (one-time reminder). This is the default for ALL reminders.
            Only use a repeating type when the user EXPLICITLY requests repetition
            with words like 'every day', 'every Monday', 'weekly', 'each morning'.
            'Tomorrow', 'next Friday', 'in 3 hours' — all of these are type='once'.
            Repeating types: 'hourly' | 'daily' | 'weekly' | 'monthly'
            interval: integer (default 1). 'Every 2 weeks' = type=weekly, interval=2.
            When in doubt — use type='once'.
        """'''

_NOTES_NEW_RECURRENCE = '''        recurrence: """
            Default: omit it — a one-time reminder. This is the default for ALL reminders.
            Only set a rule when the user EXPLICITLY requests repetition with words like
            'every day', 'every Monday', 'weekly', 'each morning', 'twice a day'.
            'Tomorrow', 'next Friday', 'in 3 hours' — all one-time, omit recurrence.

            Format: an RFC 5545 RRULE string without DTSTART. 'due' is the anchor and the
            first fire; the time of day comes from 'due' unless BYHOUR overrides it.
              FREQ=DAILY                        — every day at the due time
              FREQ=DAILY;INTERVAL=2             — every second day
              FREQ=WEEKLY;BYDAY=TU,FR           — Tuesdays and Fridays
              FREQ=WEEKLY;INTERVAL=2;BYDAY=MO   — every other Monday
              FREQ=DAILY;BYHOUR=8,20;BYMINUTE=0 — twice a day, 08:00 and 20:00
              FREQ=MONTHLY;BYDAY=-1SU           — the last Sunday of the month
              FREQ=MONTHLY;BYMONTHDAY=1,15      — the 1st and the 15th
              FREQ=MONTHLY;BYMONTHDAY=-1        — the last day of the month

            ONE reminder carries the WHOLE schedule. Never create duplicates to cover
            several weekdays or several times of day — that is what the rule expresses.
            COUNT and UNTIL are rejected: a reminder ends by being deleted, not by expiring.
            An invalid rule comes back as a tool error naming the reason — fix the rule and
            retry. Never fall back to duplicates or to a schedule the user did not ask for.
        """
        clear_recurrence: """
            update_self_reminder only. true turns a repeating reminder into a one-time one:
            it fires once more at 'due' and is then deleted. Use it for 'stop repeating',
            'just once more', 'make it a one-off'.
            Passing recurrence and clear_recurrence together is an error — choose one.
            To CHANGE a schedule, pass the new rule in recurrence; it replaces the old one.
        """
        reading_current_state: """
            The active_reminders block carries every stored field: the rule verbatim
            (shown as 'rrule:'), the execution complexity, and the last fire time.
            Read it before any update — state the current schedule and change only what
            the user asked for, instead of overwriting the rest.
        """'''

_NOTES_OLD_FIELDS = (
    "            Only YOUR schema matters: note_id, text, instruction, due, recurrence, complexity."
)
_NOTES_NEW_FIELDS = (
    "            Only YOUR schema matters: note_id, text, instruction, due, recurrence,\n"
    "            clear_recurrence, complexity."
)

# --------------------------------------------------------------------------- #
# PROTOCOL_SMART_AGENT_SELECTION                                               #
# --------------------------------------------------------------------------- #

_SMART_OLD_HOW = '''        how: [
            "Pass the full reminder request as query — what to surface, when, and the full context needed to execute.",
            "Include the exact time in the user's local timezone.",
            "The instruction fires in a new session with no memory of this conversation — include everything relevant.",
            "For updates or deletes: include the note_id from the working_memory pending_notes block.",
        ]

        anti_patterns: [
            "❌ DON'T use for user's own to-do list — that's manage_user_tasks",
            "❌ DON'T pass a bare query without topic and time",
            "❌ DON'T omit context — the instruction is the only input the executor will receive",
            "❌ DON'T fabricate a note_id — read it from working_memory pending_notes"
        ]'''

_SMART_NEW_HOW = '''        schedules: """
            One reminder holds any repeat schedule: several weekdays, several times a day,
            every other week, the last Sunday of the month, specific days of the month.
            State the schedule in full in the query — the specialist encodes it as an
            RFC 5545 rule. NEVER split a repeating schedule across several reminders.
            Everything stays editable afterwards: the schedule, the fire time, the execution
            depth, and stopping repetition altogether. The active_reminders block shows each
            reminder's current rule verbatim — quote it when asking for a change.
        """

        how: [
            "Pass the full reminder request as query — what to surface, when, and the full context needed to execute.",
            "Include the exact time in the user's local timezone.",
            "The instruction fires in a new session with no memory of this conversation — include everything relevant.",
            "For a repeating reminder, state the whole schedule in one delegation — every day it should fire, every time of day.",
            "For updates or deletes: include the note_id from the active_reminders block.",
        ]

        anti_patterns: [
            "❌ DON'T use for user's own to-do list — that's manage_user_tasks",
            "❌ DON'T pass a bare query without topic and time",
            "❌ DON'T omit context — the instruction is the only input the executor will receive",
            "❌ DON'T create one reminder per weekday or per time of day — one rule covers them all",
            "❌ DON'T fabricate a note_id — read it from active_reminders"
        ]'''


_EDITS = {
    "COGNITIVE_PROCESS_NOTES": [
        (_NOTES_OLD_RECURRENCE, _NOTES_NEW_RECURRENCE),
        (_NOTES_OLD_FIELDS, _NOTES_NEW_FIELDS),
    ],
    "PROTOCOL_SMART_AGENT_SELECTION": [
        (_SMART_OLD_HOW, _SMART_NEW_HOW),
    ],
}


async def _run(mode: str, backup_path: str | None) -> int:
    env = EnvironmentConfig()
    db = firestore.AsyncClient(
        database=os.environ.get("FIRESTORE_DATABASE", "us-production")
    )
    collection = f"{env.domain_prompt_tokens_collection}_system"

    if mode == "revert":
        payload = json.loads(Path(backup_path).read_text())
        for token_id, content in payload["tokens"].items():
            await db.collection(collection).document(token_id).update({"content": content})
            print(f"↩️  restored {token_id}")
        return 0

    originals: dict[str, str] = {}
    patched: dict[str, str] = {}
    for token_id, edits in _EDITS.items():
        doc = await db.collection(collection).document(token_id).get()
        if not doc.exists:
            print(f"❌ {token_id}: not found in {collection}")
            return 1
        content = (doc.to_dict() or {}).get("content", "")
        originals[token_id] = content
        new_content = content
        for old, new in edits:
            if old not in new_content:
                print(f"❌ {token_id}: anchor not found — token was re-worded, patch by hand:\n{old[:120]}…")
                return 1
            new_content = new_content.replace(old, new, 1)
        patched[token_id] = new_content
        delta = len(new_content) - len(content)
        print(f"✅ {token_id}: {len(edits)} edit(s) applied, {delta:+d} chars")

    if mode == "dry-run":
        print("\n(dry run — nothing written)")
        return 0

    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = _BACKUP_DIR / f"prompt_tokens_backup_{stamp}.json"
    backup.write_text(json.dumps({"collection": collection, "tokens": originals}, indent=2))
    print(f"\n💾 backup → {backup}")

    for token_id, content in patched.items():
        await db.collection(collection).document(token_id).update(
            {"content": content, "updated_at": datetime.now(timezone.utc)}
        )
        print(f"📤 wrote {token_id}")
    print("\nPrompt cache TTL is 24h — new sessions pick this up as it expires.")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--apply", action="store_true")
    group.add_argument("--revert", metavar="BACKUP_JSON")
    args = ap.parse_args()

    mode = "revert" if args.revert else ("apply" if args.apply else "dry-run")
    sys.exit(asyncio.run(_run(mode, args.revert)))


if __name__ == "__main__":
    main()
