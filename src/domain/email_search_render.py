"""
Email search render — the result block Smart reads (email index v2 §5.4, §2.5; search slice 4). Pure: no I/O.

The lines are the card's own result lines (`email_card_text.render_result_line`, code-built, at most 350 characters),
a folded group's line carries a `+ N more of the same kind` line under it. The untrusted-content wrapper is the
caller's: the result carries its text as `sections`, each either the tool's own words (`SECTION_TOOL`: the heads, the
absent, count and recard lines) or mail text (`SECTION_MAIL`: the card lines and their fold lines), so the caller wraps
only the mail text (formatter rule P14); joined by new lines the sections are the text, byte for byte.

**Ranked** result: the head (what a matched: line means), the lines of rank groups 0-3 capped by group (0 up to ten
lines, groups 1-3 together up to eight; `scope=all` raises groups 0-3 to fifty together), the meaning-only section
(up to five lines) under its own head, each request word no card holds, one line counting the cards that match the
rest, the exact-footer when an exact hit stands first, how many matches are not shown, and the recard line. **List**
result (a request that names only a period): the count line, the card caveat (what a card line can be trusted for, as
the ranked head says it), then every card of the period, newest first, folded, at most LIST_LINES_MAX lines. **Nothing**: the final answer, the absent words, the recard line.

This module holds the result value and its two renderers: one shape, one waiver (as `drive_search`).

Public interface:
    SECTION_TOOL, SECTION_MAIL, ResultSection
    MailSearchResult
    render_ranked(result, lines, meaning_lines, *, total, scope_all, uncarded, has_topic) -> MailSearchResult
    render_list(window, lines, *, count, uncarded) -> MailSearchResult
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Mapping, Sequence, Tuple

from .email_card import EmailCard
from .email_card_text import render_result_line
from .email_card_time import _MONTH_ABBR
from .email_search_fold import FoldedLine
from .email_search_merge import GROUP_ABOUT, GROUP_MEANING, RankedResult
from .email_search_params import LIST_LINES_MAX, SCOPE_ALL_LINES, SHOWN_FIRST, SHOWN_MEANING, SHOWN_REST
from .email_search_texts import (
    ABSENT_LINE, CARD_CAVEAT, FOLD_LINE, FOOTER_EXACT, HEAD_MEANING, HEAD_RANKED, LIST_ALL_LINE, LIST_ALL_LINE_SINGULAR,
    LIST_FIRST_LINE, LIST_NONE_LINE, NOTHING_LINE, RECARD_LINE, RECARD_LINE_SINGULAR, SHOWING_LINE, WITHOUT_MANY,
    WITHOUT_MANY_SINGULAR, WITHOUT_ONE, WITHOUT_ONE_SINGULAR,
)
from .email_search_time import TimeWindow

MODE_LIST, MODE_RANKED, MODE_NOTHING = "list", "ranked", "nothing"
SECTION_TOOL, SECTION_MAIL = "tool", "mail"


@dataclass(frozen=True)
class ResultSection:
    """A run of result lines: `kind` is SECTION_TOOL (the tool's own words) or SECTION_MAIL (text drawn from mail)."""
    kind: str
    text: str


@dataclass(frozen=True)
class MailSearchResult:
    """The rendered block and what the harness reads: the `mode`, the ids on a line of their own (in order), the ids
    each fold line stands for, every candidate id ranked before the caps, and how many mails match (rank groups 0-3
    for a ranked result, the mails of the period for a list). `sections` are the text split by who wrote it (module
    docstring); `shown` are the cards on a line of their own, in order."""
    text: str
    mode: str
    shown_ids: Tuple[str, ...]
    folded_ids: Mapping[str, Tuple[str, ...]]
    ranked_ids: Tuple[str, ...]
    total: int
    sections: Tuple[ResultSection, ...] = ()
    shown: Tuple[EmailCard, ...] = ()


def render_ranked(result: RankedResult, lines: Sequence[FoldedLine], meaning_lines: Sequence[FoldedLine], *,
                  total: int, scope_all: bool, uncarded: int, has_topic: bool = False) -> MailSearchResult:
    """The ranked block (module docstring). `lines` are the folded rank groups 0-3, `meaning_lines` group 4."""
    ranked_ids = tuple(c.card.mail.email_id for c in result.ranked)
    shown = _cap(lines, scope_all)
    meaning = tuple(meaning_lines[:SHOWN_MEANING])
    if not shown and not meaning:
        return _nothing(result, uncarded, ranked_ids)
    parts: List[Tuple[str, List[str]]] = [(SECTION_TOOL, [HEAD_RANKED]), (SECTION_MAIL, _render_lines(shown))]
    if meaning:
        parts += [(SECTION_TOOL, [HEAD_MEANING]), (SECTION_MAIL, _render_lines(meaning))]
    tail: List[str] = [ABSENT_LINE.format(word=w) for w in result.absent]
    tail.extend(_without(result))
    if has_topic and any(line.group == GROUP_ABOUT for line in shown):
        tail.append(FOOTER_EXACT)
    shown_mails = sum(1 + len(line.folded) for line in shown)
    if shown_mails < total:
        tail.append(SHOWING_LINE.format(shown=shown_mails, total=total))
    tail.extend(_recard(uncarded))
    parts.append((SECTION_TOOL, tail))
    return _result(parts, MODE_RANKED, (*shown, *meaning), ranked_ids, total)


def render_list(window: TimeWindow, lines: Sequence[FoldedLine], *, count: int, uncarded: int) -> MailSearchResult:
    """The list block of a request that names only a period (module docstring)."""
    shown = tuple(lines[:LIST_LINES_MAX])
    ids = tuple(line.card.mail.email_id for line in lines)
    if count <= 0 or not shown:
        return _result([(SECTION_TOOL, [LIST_NONE_LINE.format(period=window.label), *_recard(uncarded)])], MODE_LIST,
                       (), ids, 0)
    shown_mails = sum(1 + len(line.folded) for line in shown)
    if shown_mails >= count:
        head = (LIST_ALL_LINE_SINGULAR if count == 1 else LIST_ALL_LINE).format(count=count, period=window.label)
    else:
        head = LIST_FIRST_LINE.format(count=count, period=window.label, shown=shown_mails)
    parts = [(SECTION_TOOL, [head, CARD_CAVEAT]), (SECTION_MAIL, _render_lines(shown)),
             (SECTION_TOOL, _recard(uncarded))]
    return _result(parts, MODE_LIST, shown, ids, count)


# -- implementation -------------------------------------------------------------------------------------------------

def _cap(lines: Sequence[FoldedLine], scope_all: bool) -> Tuple[FoldedLine, ...]:
    """Rank groups 0-3 under their caps, in rank order (module docstring)."""
    if scope_all:
        return tuple(lines[:SCOPE_ALL_LINES])
    first = [line for line in lines if line.group == GROUP_ABOUT][:SHOWN_FIRST]
    rest = [line for line in lines if GROUP_ABOUT < line.group < GROUP_MEANING][:SHOWN_REST]
    keep = {id(line) for line in (*first, *rest)}
    return tuple(line for line in lines if id(line) in keep)


def _render_lines(lines: Sequence[FoldedLine]) -> List[str]:
    out: List[str] = []
    for line in lines:
        out.append(render_result_line(line.card, line.matched))
        if line.folded:
            out.append(FOLD_LINE.format(count=len(line.folded), domain=line.card.mail.from_domain,
                                        span=_span(line.folded)))
    return out


def _span(cards: Sequence[EmailCard]) -> str:
    """"Mar 2026", or "Jan 2025 – Oct 2026": the months the folded mail was sent in."""
    days = sorted(c.mail.sent_day for c in cards)

    def month(d: date) -> str:
        return f"{_MONTH_ABBR[d.month - 1]} {d.year}"
    first, last = month(days[0]), month(days[-1])
    return first if first == last else f"{first} – {last}"


def _without(result: RankedResult) -> List[str]:
    if not result.absent or result.without is None:
        return []
    many = len(result.absent) > 1
    if result.without == 1:
        return [WITHOUT_MANY_SINGULAR if many else WITHOUT_ONE_SINGULAR]
    return [(WITHOUT_MANY if many else WITHOUT_ONE).format(count=result.without)]


def _recard(uncarded: int) -> List[str]:
    if uncarded <= 0:
        return []
    return [RECARD_LINE_SINGULAR if uncarded == 1 else RECARD_LINE.format(count=uncarded)]


def _nothing(result: RankedResult, uncarded: int, ranked_ids: Tuple[str, ...]) -> MailSearchResult:
    out = [NOTHING_LINE, *(ABSENT_LINE.format(word=w) for w in result.absent), *_without(result), *_recard(uncarded)]
    return _result([(SECTION_TOOL, out)], MODE_NOTHING, (), ranked_ids, 0)


def _result(parts: Sequence[Tuple[str, List[str]]], mode: str, lines: Sequence[FoldedLine],
            ranked_ids: Tuple[str, ...], total: int) -> MailSearchResult:
    """The result of the sections' lines: an empty run is dropped and two runs of one kind are one section, so the
    sections joined by new lines are the text."""
    sections: List[ResultSection] = []
    for kind, run in parts:
        if not run:
            continue
        text = "\n".join(run)
        if sections and sections[-1].kind == kind:
            sections[-1] = ResultSection(kind, f"{sections[-1].text}\n{text}")
        else:
            sections.append(ResultSection(kind, text))
    shown_ids, folded = _ids(lines)
    return MailSearchResult("\n".join(s.text for s in sections), mode, shown_ids, folded, ranked_ids, total,
                            tuple(sections), tuple(line.card for line in lines))


def _ids(lines: Sequence[FoldedLine]) -> Tuple[Tuple[str, ...], Dict[str, Tuple[str, ...]]]:
    shown = tuple(line.card.mail.email_id for line in lines)
    folded = {line.card.mail.email_id: tuple(c.mail.email_id for c in line.folded) for line in lines if line.folded}
    return shown, folded
