"""
Drive tier render — the text Smart reads for a Search v2 query (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §25.5.6,
verbatim contract §25.13.6). Pure; `drive_search.render_results` calls it for every query result that carries tiers.

Layout: the reference reminder, the envelope lines, the searched line, the coverage line, the
tiers in order (About, Mentioned in, By translation, Labelled, Related), and the footers. A tier shows at most its
shown count of cards; the rest are "showing n of N" and up to GROUP_LINES group lines (top folder × kind × months) in
one wrapper. Past RESULT_CHARS_MAX the lowest tiers' shown cards are cut first, and the result says so.

Public interface:
    render_tiered(result) -> str
    group_lines(members, tier) -> (str, ...)
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from .drive_index_params import CARD_RENDER_MAX, GROUP_LINES, RESULT_CHARS_MAX
from .drive_match import MatchTier
from .drive_search import (
    REFERENCE_REMINDER, DriveSearchCard, DriveSearchResult, accuracy_line, capture_line, escape_text, index_line,
    scene_line, stale_line, wrap_untrusted,
)
from .drive_search_texts import (
    ABOUT_HEADING, ABSENT_IN_SCOPE_LINE, ABSENT_LINE, CONJUNCTION_TERM, COVERAGE_COMPLETE, COVERAGE_INACCESSIBLE,
    COVERAGE_INCOMPLETE,
    COVERAGE_NOT_DESCRIBABLE, COVERAGE_NOT_DESCRIBED, DATE_EXPIRES, DATE_LINE, FOOTER_EXACT, FOOTER_EXACT_SEARCHED,
    FOOTER_NO_CONJUNCTION, FOOTER_NO_CONJUNCTION_SEARCHED, GROUP_LINE, KEYS_CUT, LABELLED_HEADING, MATCHED_LINE,
    MENTIONED_HEADING, NO_PASSAGES_NOTE, PASSAGE_LINE, RELATED_HEADING, RELATED_SMALL_NOTE, SEARCHED_ABSENT_IN_SCOPE_LINE,
    SEARCHED_ABSENT_LINE, SEARCHED_COMMON, SEARCHED_IGNORED, SEARCHED_LINE, SHOWING, SHOWING_AT_LEAST, SIMILAR_NAME,
    TRANSLATED_HEADING, VALID_UNTIL_LINE, WITHOUT_AT_LEAST_LINE, WITHOUT_LINE,
)
from .drive_tier_result import CoverageFacts, GroupMember, TierBlock, TieredResult

TIER_ORDER: Tuple[MatchTier, ...] = (MatchTier.ABOUT, MatchTier.MENTIONED, MatchTier.TRANSLATED, MatchTier.LABELLED,
                                     MatchTier.RELATED)
_HEADINGS = {MatchTier.MENTIONED: MENTIONED_HEADING, MatchTier.TRANSLATED: TRANSLATED_HEADING,
             MatchTier.LABELLED: LABELLED_HEADING}
_GROUP_WORD = {MatchTier.ABOUT: "matching", MatchTier.MENTIONED: "mentioning", MatchTier.TRANSLATED: "translated",
               MatchTier.LABELLED: "labelled"}
_RELATED_CARD_CHARS = 300
_RELATED_NOTE_CHARS = 120     # DRIVE_FILE_NOTES_RFC §6.2: a Related card's note line, cut
_CUT_LINE = "Shortened to fit: fewer files are shown as cards; their counts and folders are above."
_EXPIRED_LINE = "{count} more {files} with these words already expired."
_NOTHING = "No matching files or folders."
_ELLIPSIS = "…"


def _one(text: str, count: int) -> str:
    """The frozen templates say "files"; one file reads as one (plural forms are P6's, §25.13.6)."""
    if count != 1:
        return text
    return text.replace("1 files match ", "1 file matches ").replace("(1 files)", "(1 file)")


def _quoted(words: Sequence[str]) -> str:
    return ", ".join(f'"{w}"' for w in words)


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _day(card: DriveSearchCard) -> str:
    return card.effective_date.date().isoformat() if card.effective_date else ""


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + _ELLIPSIS


# -- header lines ----------------------------------------------------------------------------------------------------

def _envelope_lines(result: DriveSearchResult) -> List[str]:
    env = result.envelope
    lines: List[str] = []
    if env.index_as_of is not None:
        lines.append(f"The index reflects the drive as of {env.index_as_of:%Y-%m-%d %H:%M} UTC.")
    else:
        lines.append("The index has not completed a first pass over the drive yet.")
    if result.folder_path is not None:
        lines.append("Searched only within the requested folder and its subfolders.")
    if env.index_full:
        lines.append("The index is full: some files have no searchable card.")
    if env.reconnect_needed:
        lines.append("The drive connection expired: these results come from the stored index. Ask the user "
                     "to reconnect the drive in the Cabinet.")
    return lines


def _searched_line(tiered: TieredResult) -> str:
    line = SEARCHED_LINE.format(concepts=_quoted(tiered.concepts))
    ignored = list(tiered.ignored) + [w for w in tiered.coverage.cut if w not in tiered.ignored]
    if ignored:
        line += SEARCHED_IGNORED.format(words=_quoted(ignored))
    if tiered.common:
        line += SEARCHED_COMMON.format(words=_quoted(tiered.common))
    return line + "."


def coverage_parts(cov: CoverageFacts, *, passages_tiers_empty: bool) -> List[str]:
    """The named parts of the coverage line (§25.5.6), in a fixed order."""
    parts: List[str] = []
    if cov.rebuilding:
        parts.append("the index is being rebuilt for a new search format, so files not rebuilt yet are not searched")
    if cov.building:
        parts.append("the index is still being built, so some files may not be in it yet")
    if cov.names_only:
        parts.append("only names and metadata were searched, not what the files contain")
    if cov.refreshing:
        parts.append("the index is being refreshed for a new search model, so meaning was not searched")
    if cov.rekeying:
        parts.append("the index is being re-keyed, so some words in the text may not be found yet")
    if cov.not_described:
        parts.append(f"{_plural(cov.not_described, 'text file is', 'text files are')} {COVERAGE_NOT_DESCRIBED}")
    if cov.not_describable:
        parts.append(f"{_plural(cov.not_describable, 'text file', 'text files')} {COVERAGE_NOT_DESCRIBABLE}")
    # Files without passage keywords BY POLICY (books, sheets, data; §25.3) are not a gap: their own words were
    # searched. They never make coverage incomplete — `_no_passages_note` names them only on an empty result (UAT
    # 2026-10-14: counting them made every answer hedge).
    if cov.reading:
        parts.append("documents are still being read")
    if cov.not_read_yet:
        parts.append(f"{_plural(cov.not_read_yet, 'document is', 'documents are')} not read yet")
    if cov.unread:
        parts.append(f"{_plural(cov.unread, 'document', 'documents')} could not be read")
    if cov.full_page:
        parts.append("a candidate page was full, so more files may match")
    if cov.unchecked:
        parts.append(f"words held by many files could not be checked in the whole scope: {_quoted(cov.unchecked)}")
    if cov.keys_cut:
        parts.append(f"{_plural(cov.keys_cut, 'shown file', 'shown files')} had words cut by a key cap")
    if cov.cut:
        parts.append(f"words past the first ones were not searched: {_quoted(cov.cut)}")
    return parts


def _coverage_line(tiered: TieredResult) -> str:
    empty = not tiered.block(MatchTier.MENTIONED) and not tiered.block(MatchTier.TRANSLATED)
    parts = coverage_parts(tiered.coverage, passages_tiers_empty=empty)
    line = COVERAGE_INCOMPLETE.format(parts="; ".join(parts)) if parts else COVERAGE_COMPLETE
    # Δ93: closed files explain a not-found that may be incomplete; with results they are noise (a matching closed
    # file carries its own access line on its card).
    if tiered.coverage.inaccessible and not any(b.total for b in tiered.blocks):
        line += f" {_plural(tiered.coverage.inaccessible, 'file', 'files').capitalize()} {COVERAGE_INACCESSIBLE}."
    return line


def _no_passages_note(tiered: TieredResult) -> Optional[str]:
    """A neutral note on books, sheets and data files (no passage keywords by policy), only when nothing exact or
    automatic matched: there it tells a not-found that those files were not searched in translation."""
    count = tiered.coverage.no_passages
    if not count or any(tiered.block(t) for t in (MatchTier.ABOUT, MatchTier.MENTIONED, MatchTier.TRANSLATED,
                                                   MatchTier.LABELLED)):
        return None
    return NO_PASSAGES_NOTE.format(files=_plural(count, "book, sheet or data file was searched by its",
                                                 "books, sheets and data files were searched by their"))


# -- cards and groups ------------------------------------------------------------------------------------------------

def card_body(card: DriveSearchCard) -> str:
    lines = [_cut(card.summary, CARD_RENDER_MAX)]
    if card.owner_note:
        lines.append(card.owner_note)          # DRIVE_FILE_NOTES_RFC §6.2: after the summary, outside its cut
    if card.sources:
        lines.append(MATCHED_LINE.format(sources="; ".join(card.sources)))
    lines.extend(card.not_matched)
    if card.passage:
        lines.append(PASSAGE_LINE.format(text=card.passage))
    until = card.valid_until.date().isoformat() if card.valid_until else ""
    day = _day(card)
    if until and card.expiry_match:
        # matched through validity words ("what expires"): the date that answers it is the expiry date (xc F5)
        lines.append(DATE_LINE.format(iso=until, source=DATE_EXPIRES))
    else:
        if day:
            lines.append(DATE_LINE.format(iso=day, source=card.date_source) if card.date_source else f"date: {day}")
        if until:
            lines.append(VALID_UNTIL_LINE.format(iso=until))
    captured = capture_line(card)
    if captured:
        lines.append(captured)                 # §26.3
    scene = scene_line(card)
    if scene:
        lines.append(scene)                    # D40
    accuracy = accuracy_line(card)
    if accuracy:
        lines.append(accuracy)                 # §26.4
    lines.append(index_line(card))
    if card.stale:
        lines.append(stale_line(card))
    if card.truncated:
        lines.append("truncated: only the beginning of the file was indexed")
    if card.keys_cut:
        lines.append(KEYS_CUT)
    if card.also_at:
        lines.append("identical copies also at: " + "; ".join(card.also_at))
    return "\n".join(lines)


def _related_body(card: DriveSearchCard) -> str:
    day = _day(card)
    tail = f" | {day}" if day else ""
    body = _cut(card.summary, _RELATED_CARD_CHARS - len(tail)) + tail
    body += f"\n{_cut(card.owner_note, _RELATED_NOTE_CHARS)}" if card.owner_note else ""
    body += f"\n{SIMILAR_NAME}" if card.similar_name else ""
    accuracy = accuracy_line(card)         # §26.4: a Related card is opened, and answers a value question, like any
    body += f"\n{accuracy}" if accuracy else ""
    return body + (f"\n{index_line(card)}" if card.inaccessible else "")   # the access line (§26.2)


def member_of(card: DriveSearchCard) -> GroupMember:
    folder = card.path.rsplit("/", 1)[0] if "/" in card.path else "/"
    month = card.effective_date.strftime("%Y-%m") if card.effective_date else ""
    return GroupMember(folder=folder, kind=card.kind, month=month)


def _kind_label(kind: str) -> str:
    return kind.upper() if kind in ("pdf", "docx", "doc", "xlsx", "xls", "csv", "pptx", "odt", "rtf", "html")\
        else kind


def group_lines(members: Sequence[GroupMember], tier: MatchTier) -> Tuple[str, ...]:
    """Up to GROUP_LINES lines, one per folder × kind (the folder a file sits in, as §25.13.6's example groups
    "Travel/Rome"), most members first: "{folder}: {count} more {tier} files ({kind}, {months})"."""
    by_group: Dict[Tuple[str, str], List[str]] = {}
    for m in members:
        by_group.setdefault((m.folder, m.kind), []).append(m.month)
    ordered = sorted(by_group.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    lines = []
    for (folder, kind), months in ordered[:GROUP_LINES]:
        dated = sorted(m for m in months if m)
        dates = "undated" if not dated else dated[0] if dated[0] == dated[-1] else f"{dated[0]}–{dated[-1]}"
        template = GROUP_LINE.replace(" files (", " file (") if len(months) == 1 else GROUP_LINE   # P6's plural
        lines.append(template.format(folder=folder, count=len(months), tier=_GROUP_WORD[tier],
                                     kinds=_kind_label(kind), dates=dates))
    return tuple(lines)


def _heading(tier: MatchTier, tiered: TieredResult, block: TierBlock, shown: int) -> str:
    if block.at_least:
        count = SHOWING_AT_LEAST.format(shown=shown, total=block.total)
    elif shown < block.total:
        count = SHOWING.format(shown=shown, total=block.total)
    else:
        count = str(block.total)
    if tier is MatchTier.ABOUT:
        return ABOUT_HEADING.format(query=escape_text(" ".join(tiered.concepts)), count=count)
    return _HEADINGS[tier].format(count=count)


# -- footers ---------------------------------------------------------------------------------------------------------

def _footers(tiered: TieredResult, any_shown: bool, exact_shown: bool, incomplete: bool = False) -> List[str]:
    """The D29 lines and the footers. `incomplete`: the coverage line names something the search could not see, so
    an absence or an exhaustive "no other file" is stated only over the files searched (xc F4)."""
    lines: List[str] = []
    absent_line = SEARCHED_ABSENT_LINE if incomplete else ABSENT_LINE
    in_scope_line = SEARCHED_ABSENT_IN_SCOPE_LINE if incomplete else ABSENT_IN_SCOPE_LINE
    if tiered.absent or tiered.absent_in_scope:
        lines += [absent_line.format(word=w) for w in tiered.absent]
        lines += [in_scope_line.format(word=w) for w in tiered.absent_in_scope]
        if tiered.without is not None:
            word = '", "'.join(tiered.absent + tiered.absent_in_scope)
            template = WITHOUT_AT_LEAST_LINE if tiered.without_at_least else WITHOUT_LINE
            lines.append(_one(template.format(word=word, count=tiered.without), tiered.without))
    elif tiered.conjunction:
        terms = ", ".join(_one(CONJUNCTION_TERM.format(word=w, count=n), n) for w, n in tiered.conjunction)
        lines.append((FOOTER_NO_CONJUNCTION_SEARCHED if incomplete else FOOTER_NO_CONJUNCTION).format(terms=terms))
    elif exact_shown:
        lines.append(FOOTER_EXACT_SEARCHED if incomplete else FOOTER_EXACT)
    return lines


# -- the whole result ------------------------------------------------------------------------------------------------

def _render(result: DriveSearchResult, tiered: TieredResult, shown: Dict[MatchTier, List[DriveSearchCard]],
            moved: Dict[MatchTier, List[GroupMember]], cut: bool) -> str:
    coverage = _coverage_line(tiered)
    out: List[str] = [REFERENCE_REMINDER, *_envelope_lines(result), _searched_line(tiered), coverage]
    for tier in TIER_ORDER:
        cards = shown.get(tier, [])
        if tier is MatchTier.RELATED:
            if cards:
                out.append(RELATED_HEADING.format(count=len(cards)))
                if tiered.related_small:
                    out.append(RELATED_SMALL_NOTE)
                out.extend(wrap_untrusted(_related_body(c), nonce=c.nonce, ref=c.ref, path=c.path) for c in cards)
            continue
        block = tiered.block(tier)
        if block is None or block.total == 0:
            continue
        out.append(_heading(tier, tiered, block, len(cards)))
        out.extend(wrap_untrusted(card_body(c), nonce=c.nonce, ref=c.ref, path=c.path) for c in cards)
        groups = group_lines(list(moved.get(tier, [])) + list(block.rest), tier)
        if groups and block_nonce(block, cards):
            out.append(wrap_untrusted("\n".join(groups), nonce=block_nonce(block, cards), ref="", path=""))
    if cut:
        out.append(_CUT_LINE)
    if tiered.expired:
        out.append(_EXPIRED_LINE.format(count=tiered.expired, files="file" if tiered.expired == 1 else "files"))
    any_shown = any(shown.get(t) for t in TIER_ORDER)
    exact_shown = bool(tiered.block(MatchTier.ABOUT) or tiered.block(MatchTier.MENTIONED))
    footers = _footers(tiered, any_shown, exact_shown, incomplete=coverage != COVERAGE_COMPLETE)
    out.extend(footers)
    note = _no_passages_note(tiered)
    if note:
        out.append(note)
    if (not any_shown and not footers and not any(b.total for b in tiered.blocks)
            and not tiered.coverage.unchecked):
        out.append(_NOTHING)
    return "\n".join(out)


def block_nonce(block: TierBlock, cards: Sequence[DriveSearchCard]) -> str:
    """The group wrapper's nonce: the block's own, else its first card's suffixed (still letters and digits)."""
    if block.nonce:
        return block.nonce
    return f"{cards[0].nonce}g" if cards else ""


def render_tiered(result: DriveSearchResult) -> str:
    """The query result Smart reads (§25.13.6). Cards past RESULT_CHARS_MAX are cut from the lowest tier first and
    join its group lines (Related ones are dropped); the result then says it was shortened."""
    tiered = result.tiered
    if tiered is None:
        raise ValueError("render_tiered needs a result with tiers")
    shown: Dict[MatchTier, List[DriveSearchCard]] = {t: [c for c in result.cards if c.tier == t.value]
                                                     for t in TIER_ORDER}
    moved: Dict[MatchTier, List[GroupMember]] = {t: [] for t in TIER_ORDER}
    cut = False
    while True:
        text = _render(result, tiered, shown, moved, cut)
        if len(text) <= RESULT_CHARS_MAX:
            return text
        tier: Optional[MatchTier] = next((t for t in reversed(TIER_ORDER) if shown[t]), None)
        if tier is None:
            return text
        card = shown[tier].pop()
        if tier is not MatchTier.RELATED:
            moved[tier].insert(0, member_of(card))
        cut = True

