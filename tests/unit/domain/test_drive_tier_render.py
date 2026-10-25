"""render_results for a Search v2 query, byte-exact against the §25.13.6 example (USER_DRIVE_INDEXING_RFC), plus the
D29, footer, migration, group, coverage and size-limit rules of §25.5.6."""
from dataclasses import replace
from datetime import datetime, timezone

from src.domain.drive_index_params import ABOUT_SHOWN, GROUP_LINES, RESULT_CHARS_MAX
from src.domain.drive_match import MatchTier
from src.domain.drive_search import (
    DATE_FILE, DATE_TAKEN, DATE_WRITTEN, REFERENCE_REMINDER, STATUS_OK, DriveSearchCard, DriveSearchEnvelope,
    DriveSearchRequest, DriveSearchResult, render_results,
)
from src.domain.drive_tier_render import group_lines
from src.domain.drive_tier_result import CoverageFacts, GroupMember, TierBlock, TieredResult

AS_OF = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)


def day(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def card(item, path, summary, tier, *, sources=(), passage="", when=None, source=None, nonce=None, kind="pdf"):
    return DriveSearchCard(ref=f"drive:{item}", item_id=item, name=path.rsplit("/", 1)[-1], path=path, kind=kind,
                           is_folder=False, summary=summary, effective_date=day(when) if when else None,
                           state="indexed", nonce=nonce or f"n{item}00000", date_source=source, tier=tier.value,
                           sources=tuple(sources), passage=passage)


def example():
    cards = (
        card("A1", "Travel/Rome/Colosseum entry.pdf",
             "Colosseum entry.pdf | PDF document | ticket | Two entry tickets to the Colosseum, Rome, 12 May 2026",
             MatchTier.ABOUT, sources=('place "Rome" (named in the file)', 'name "tickets"'), when="2026-05-12",
             source=DATE_WRITTEN),
        card("B2", "Notes/trip.md", "trip.md | note (Markdown) | notes | Packing list and plans for a week in Rome",
             MatchTier.MENTIONED, sources=('words "Rome", "tickets" in one passage',), kind="markdown",
             passage="pick up the tickets for the Vatican museums on Tuesday, then walk to"),
        card("C3", "Viajes/reserva.pdf", "reserva.pdf | PDF document | booking | Hotel booking confirmation",
             MatchTier.TRANSLATED, sources=('passage keyword "Rome", from "reserva en Roma, 3 noches"',),
             passage="Confirmamos su reserva en Roma, 3 noches, del 10 al 13 de mayo"),
        card("D4", "Travel/train.pdf", "train.pdf | PDF document | ticket", MatchTier.LABELLED,
             sources=('type label "ticket" (automatic)', 'place "Rome" (named in the file)')),
        card("E5", "Travel/bus.pdf", "bus.pdf | PDF document | ticket", MatchTier.LABELLED),
        card("F6", "Photos/IMG_2210.jpg", "IMG_2210.jpg | image | a square with a fountain at night",
             MatchTier.RELATED, when="2026-05-11", source=DATE_TAKEN, kind="image"),
    )
    rest = tuple(GroupMember("Travel/Rome", "pdf", "2026-05") for _ in range(3))
    tiered = TieredResult(
        concepts=("rome", "tickets"), ignored=("all", "my"), coverage=CoverageFacts(not_described=4, unread=1),
        blocks=(TierBlock(MatchTier.ABOUT, 1), TierBlock(MatchTier.MENTIONED, 1), TierBlock(MatchTier.TRANSLATED, 1),
                TierBlock(MatchTier.LABELLED, 5, rest=rest, nonce="ngroup0000"), TierBlock(MatchTier.RELATED, 1)))
    return DriveSearchResult(status=STATUS_OK, cards=cards, envelope=DriveSearchEnvelope(index_as_of=AS_OF),
                             request=DriveSearchRequest(query="all my rome tickets"), tiered=tiered)


EXPECTED = "\n".join([
    REFERENCE_REMINDER,
    "The index reflects the drive as of 2026-10-20 08:00 UTC.",
    'Searched names (several spellings and word forms), file descriptions, document text and passage keywords for: '
    '"rome", "tickets"; ignored: "all", "my".',
    "Coverage: incomplete — 4 text files are not described yet; 1 document could not be read.",
    'About "rome tickets" (1):',
    '<drive_document nonce="nA100000" ref="drive:A1" path="Travel/Rome/Colosseum entry.pdf">',
    "Colosseum entry.pdf | PDF document | ticket | Two entry tickets to the Colosseum, Rome, 12 May 2026",
    'matched: place "Rome" (named in the file); name "tickets"',
    "date: 2026-05-12 (written in the document)",
    "index: indexed",
    '</drive_document nonce="nA100000">',
    "Mentioned in (1):",
    '<drive_document nonce="nB200000" ref="drive:B2" path="Notes/trip.md">',
    "trip.md | note (Markdown) | notes | Packing list and plans for a week in Rome",
    'matched: words "Rome", "tickets" in one passage',
    "passage: … pick up the tickets for the Vatican museums on Tuesday, then walk to …",
    "index: indexed",
    '</drive_document nonce="nB200000">',
    "By translation (automatic) (1):",
    '<drive_document nonce="nC300000" ref="drive:C3" path="Viajes/reserva.pdf">',
    "reserva.pdf | PDF document | booking | Hotel booking confirmation",
    'matched: passage keyword "Rome", from "reserva en Roma, 3 noches"',
    "passage: … Confirmamos su reserva en Roma, 3 noches, del 10 al 13 de mayo …",
    "index: indexed",
    '</drive_document nonce="nC300000">',
    "Labelled (automatic) (showing 2 of 5):",
    '<drive_document nonce="nD400000" ref="drive:D4" path="Travel/train.pdf">',
    "train.pdf | PDF document | ticket",
    'matched: type label "ticket" (automatic); place "Rome" (named in the file)',
    "index: indexed",
    '</drive_document nonce="nD400000">',
    '<drive_document nonce="nE500000" ref="drive:E5" path="Travel/bus.pdf">',
    "bus.pdf | PDF document | ticket",
    "index: indexed",
    '</drive_document nonce="nE500000">',
    '<drive_document nonce="ngroup0000" ref="" path="">',
    "Travel/Rome: 3 more labelled files (PDF, 2026-05)",
    '</drive_document nonce="ngroup0000">',
    "Related by meaning only, unverified (1):",
    '<drive_document nonce="nF600000" ref="drive:F6" path="Photos/IMG_2210.jpg">',
    "IMG_2210.jpg | image | a square with a fountain at night | 2026-05-11",
    '</drive_document nonce="nF600000">',
    # xc F4: the coverage line is incomplete, so the exhaustive footer speaks of searched files only (RFC §25.13.6)
    "No other searched file is about or mentions all of these words.",
])


def test_the_section_25_13_6_example_byte_for_byte():
    assert render_results(example()) == EXPECTED


def test_d29_lines():
    tiered = TieredResult(concepts=("vaccination", "dog"), absent=("vaccination",), without=3)
    text = render_results(replace(example(), cards=(), tiered=tiered))
    assert text.endswith('No file holds "vaccination".\nWithout "vaccination": 3 files match the other words.')
    at_least = replace(tiered, without_at_least=True)
    assert render_results(replace(example(), cards=(), tiered=at_least)).endswith(
        'Without "vaccination": at least 3 files match the other words.')


def test_a_word_absent_only_in_scope():
    tiered = TieredResult(concepts=("rome",), absent_in_scope=("rome",))
    assert render_results(replace(example(), cards=(), tiered=tiered)).endswith('No file in this scope holds "rome".')


def test_empty_conjunction_footer():
    tiered = TieredResult(concepts=("coimbra", "trip"), conjunction=(("coimbra", 41), ("trip", 2)))
    assert render_results(replace(example(), cards=(), tiered=tiered)).endswith(
        'No file matches all of: "coimbra" (41 files), "trip" (2 files).')


def test_coverage_complete_and_never_says_final():
    r = example()
    text = render_results(replace(r, tiered=replace(r.tiered, coverage=CoverageFacts())))
    assert "Coverage: complete." in text and "final" not in text.lower()


def test_no_passage_keywords_only_when_the_passage_tiers_are_empty():
    # Spec change (UAT 2026-10-14): policy-excluded files are a neutral note on an empty result, never a coverage gap.
    r = example()
    cov = CoverageFacts(no_passages=2)
    note = "2 books, sheets and data files were searched by their own words only"
    assert note not in render_results(replace(r, tiered=replace(r.tiered, coverage=cov)))
    blocks = tuple(b for b in r.tiered.blocks if b.tier not in (
        MatchTier.ABOUT, MatchTier.MENTIONED, MatchTier.TRANSLATED, MatchTier.LABELLED))
    cards = tuple(c for c in r.cards if c.tier not in ("about", "mentioned", "translated", "labelled"))
    text = render_results(replace(r, cards=cards, tiered=replace(r.tiered, coverage=cov, blocks=blocks)))
    assert note in text and "no passage keywords" not in text and "Coverage: complete." in text


def test_common_and_cut_words_are_named_in_the_searched_line():
    r = example()
    tiered = replace(r.tiered, common=("photo",), coverage=CoverageFacts(cut=("ninth",)))
    text = render_results(replace(r, tiered=tiered))
    assert '; ignored: "all", "my", "ninth"; ignored as common: "photo".' in text


def test_nothing_found():
    text = render_results(replace(example(), cards=(), tiered=TieredResult(concepts=("zebra",))))
    assert text.endswith("No matching files or folders.")


def test_group_lines_cap_and_date_ranges():
    members = [GroupMember(f"F{i}", "pdf", f"2025-0{1 + i % 3}") for i in range(GROUP_LINES + 2) for _ in range(2)]
    lines = group_lines(members, MatchTier.ABOUT)
    assert len(lines) == GROUP_LINES and lines[0] == "F0: 2 more matching files (PDF, 2025-01)"
    assert group_lines([GroupMember("A", "image", "2024-01"), GroupMember("A", "image", "2025-03")],
                       MatchTier.LABELLED) == ("A: 2 more labelled files (image, 2024-01–2025-03)",)


def test_the_size_limit_cuts_the_lowest_tiers_first_and_says_so():
    r = example()
    big = "x" * 690
    about = tuple(card(f"A{i}", f"Docs/a{i}.pdf", big, MatchTier.ABOUT, sources=('name "rome"',),
                       passage="p" * 300) for i in range(ABOUT_SHOWN))
    labelled = tuple(card(f"L{i}", f"Docs/l{i}.pdf", big, MatchTier.LABELLED, passage="q" * 900) for i in range(8))
    tiered = replace(r.tiered, blocks=(TierBlock(MatchTier.ABOUT, ABOUT_SHOWN), TierBlock(MatchTier.LABELLED, 8)))
    text = render_results(replace(r, cards=about + labelled, tiered=tiered))
    assert len(text) <= RESULT_CHARS_MAX
    assert "Shortened to fit" in text
    assert "Labelled (automatic) (showing" in text
    assert text.count("ref=\"drive:A") == ABOUT_SHOWN   # About kept whole while a lower tier could give way
    assert "more labelled files" in text


def test_date_without_a_source_and_file_date():
    r = example()
    c = replace(r.cards[0], date_source=DATE_FILE)
    assert "date: 2026-05-12 (file date)" in render_results(replace(r, cards=(c,) + r.cards[1:]))


def test_one_file_is_singular():
    tiered = TieredResult(concepts=("vaccination", "dog"), absent=("vaccination",), without=1)
    assert render_results(replace(example(), cards=(), tiered=tiered)).endswith(
        'Without "vaccination": 1 file matches the other words.')
    tiered = TieredResult(concepts=("coimbra", "trip"), conjunction=(("coimbra", 1), ("trip", 2)))
    assert '"coimbra" (1 file), "trip" (2 files)' in render_results(replace(example(), cards=(), tiered=tiered))
