"""
Price consensus: decide whether a billing.py price is trustworthy.

Pure logic, no I/O and no import-time side effects, so it is unit-testable
(tests/unit/validation/test_price_consensus.py). The fetching lives in check_pricing.py.

WHY THIS EXISTS
---------------
The audit used to compare billing.py against **OpenRouter**, which is a reseller quoting
its own rates — not the price we are billed. On 2026-07-29 that produced 6 wrong verdicts
out of 8: it called `gpt-5.6-luna` ($1.00/$6.00) a mismatch because OpenRouter sells it at
$0.50/$3.00, and it flagged the `gpt-5.6-*` cache-write multiplier as wrong because
OpenRouter simply does not publish one. Meanwhile a stale hardcoded alias map produced
false ✅ verdicts for the Gemini `*-latest` entries.

The replacement rule, per owner decision:

    two independent catalogs agree  → CONFIRMED
    they disagree                   → REVIEW (never silently pick one)
    only one covers the model       → REVIEW (a single source is a lead, not a fact)
    nobody covers it                → UNCOVERED (must NOT read as "fine")
    a dated schedule entry exists   → the schedule wins over consensus

Sources are LiteLLM's `model_prices_and_context_window.json` and models.dev's `api.json`.
Both track provider list prices, which is what we are actually charged.

INDEPENDENCE CAVEAT: both are community catalogs curated from provider pricing pages, so
they share a failure mode — each carries the *currently posted* price with no expiry. That
is exactly why both quote Claude Sonnet 5 at its introductory $2/$10 and neither knows the
rate reverts on 2026-09-01. Hence PRICE_SCHEDULE: agreement answers "what does it cost
today", not "what should our table encode".
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Tuple

# (input_per_1M, output_per_1M)
Price = Tuple[float, float]

CONFIRMED = "confirmed"            # >=2 sources agree and match billing.py
CONSENSUS_DIFFERS = "consensus_differs"   # >=2 sources agree and contradict billing.py
SOURCES_DISAGREE = "sources_disagree"     # sources contradict each other
SINGLE_SOURCE = "single_source"           # only one source covers it
UNCOVERED = "uncovered"                   # no source covers it
SCHEDULE_DRIFT = "schedule_drift"         # a scheduled change is due and we did not apply it
SCHEDULE_STALE = "schedule_stale"         # consensus contradicts our own schedule

NEEDS_REVIEW = frozenset({
    CONSENSUS_DIFFERS, SOURCES_DISAGREE, SINGLE_SOURCE, UNCOVERED,
    SCHEDULE_DRIFT, SCHEDULE_STALE,
})


@dataclass(frozen=True)
class Verdict:
    status: str
    detail: str
    consensus: Optional[Price] = None
    # Next known change, when the schedule declares one in the future.
    upcoming: Optional[Tuple[date, Price]] = None

    @property
    def needs_review(self) -> bool:
        return self.status in NEEDS_REVIEW


# ---------------------------------------------------------------------------
# Known dated price changes — these OVERRIDE catalog consensus.
#
# Add an entry whenever a provider announces a promotional or scheduled rate: the catalogs
# publish today's number with no expiry, so without this the audit would push us onto a
# promo price and then silently rot the day it ends.
#
# Each list is (effective_from, input, output), ascending. The entry in force today is the
# last one whose date has arrived; billing.py is expected to hold THAT price, so cost
# reports match the invoice.
# ---------------------------------------------------------------------------
PRICE_SCHEDULE: Dict[str, List[Tuple[date, Price]]] = {
    # Introductory pricing, then standard.
    # https://www.anthropic.com/news/claude-sonnet-5
    "claude-sonnet-5": [
        (date(2026, 1, 1), (2.00, 10.00)),
        (date(2026, 9, 1), (3.00, 15.00)),
    ],
}

# Models where billing.py DELIBERATELY holds the final scheduled price rather than the one
# in force today. Documented policy (see the comment on `claude-sonnet-5` in billing.py):
# track the standard list price so cost is never UNDER-reported, accepting that spend reads
# high while an introductory rate lasts, and that no edit is needed when it expires.
#
# Without this set, the audit would report the deliberate choice as `schedule_drift` and
# invite someone to "fix" a decision that was made on purpose. The trade-off is stated in
# the verdict instead, so the over-reporting stays visible.
HOLD_FINAL_PRICE = frozenset({"claude-sonnet-5"})


def scheduled_price(model: str, today: date) -> Optional[Price]:
    """Price the schedule says is in force on ``today``, or None if unscheduled."""
    entries = PRICE_SCHEDULE.get(model)
    if not entries:
        return None
    in_force = [p for d, p in sorted(entries) if d <= today]
    return in_force[-1] if in_force else None


def next_scheduled_change(model: str, today: date) -> Optional[Tuple[date, Price]]:
    """The next future change the schedule declares, or None."""
    entries = PRICE_SCHEDULE.get(model)
    if not entries:
        return None
    future = [(d, p) for d, p in sorted(entries) if d > today]
    return future[0] if future else None


def resolve_verdict(
    model: str,
    ours: Price,
    quotes: Dict[str, Optional[Price]],
    today: date,
) -> Verdict:
    """Judge one billing.py entry.

    model:  the key to judge. Callers MUST pass the *resolved* concrete model id for
            ``*-latest`` aliases — catalogs key on concrete models, and an alias has no
            stable price (each catalog resolves it to a different generation, which is
            precisely why the three Gemini alias entries came back as SOURCES_DISAGREE).
    ours:   the price currently in billing.py.
    quotes: {source_name: price or None}. None means "this source does not cover it".
    """
    covered = {src: p for src, p in quotes.items() if p is not None}
    upcoming = next_scheduled_change(model, today)

    if not covered:
        return Verdict(UNCOVERED,
                       "no catalog covers this model — verify at the provider by hand",
                       upcoming=upcoming)
    if len(covered) == 1:
        src, price = next(iter(covered.items()))
        return Verdict(SINGLE_SOURCE,
                       f"only {src} covers it ({_fmt(price)}) — a lead, not a fact",
                       upcoming=upcoming)

    distinct = set(covered.values())
    if len(distinct) > 1:
        quoted = ", ".join(f"{s}={_fmt(p)}" for s, p in sorted(covered.items()))
        return Verdict(SOURCES_DISAGREE,
                       f"catalogs contradict each other ({quoted}) — for a *-latest key "
                       f"this usually means the alias was not resolved first",
                       upcoming=upcoming)

    consensus = distinct.pop()
    due = scheduled_price(model, today)

    if due is not None:
        if consensus != due:
            return Verdict(SCHEDULE_STALE,
                           f"our schedule says {_fmt(due)} is in force but the catalogs "
                           f"agree on {_fmt(consensus)} — the schedule needs updating",
                           consensus=consensus, upcoming=upcoming)

        if model in HOLD_FINAL_PRICE:
            final = sorted(PRICE_SCHEDULE[model])[-1][1]
            if ours == final:
                note = (f"deliberately holds the final scheduled price {_fmt(final)}"
                        if final == due else
                        f"deliberately holds the final scheduled price {_fmt(final)}; "
                        f"today's actual is {_fmt(due)}, so this model's spend reads "
                        f"{final[1] / due[1]:.2f}x high until then (documented policy)")
                return Verdict(CONFIRMED, note, consensus=consensus, upcoming=upcoming)
            return Verdict(SCHEDULE_DRIFT,
                           f"policy is to hold the final scheduled price {_fmt(final)} but "
                           f"billing.py holds {_fmt(ours)}",
                           consensus=consensus, upcoming=upcoming)

        if ours != due:
            return Verdict(SCHEDULE_DRIFT,
                           f"scheduled price is {_fmt(due)} as of today but billing.py "
                           f"holds {_fmt(ours)} — update billing.py",
                           consensus=consensus, upcoming=upcoming)
        return Verdict(CONFIRMED,
                       f"matches the scheduled price {_fmt(due)}",
                       consensus=consensus, upcoming=upcoming)

    if consensus != ours:
        return Verdict(CONSENSUS_DIFFERS,
                       f"catalogs agree on {_fmt(consensus)}, billing.py holds {_fmt(ours)}",
                       consensus=consensus, upcoming=upcoming)
    return Verdict(CONFIRMED, "both catalogs agree with billing.py",
                   consensus=consensus, upcoming=upcoming)


def _fmt(p: Price) -> str:
    return f"${p[0]:g}/${p[1]:g}"
