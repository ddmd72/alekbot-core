"""
Drive tier explain — the match sources and the passage a Search v2 card shows (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md
§25.5.6, texts §25.13.6). Pure: no I/O.

A card says how each query word matched, with the printed form the file holds: `place "Rome" (named in the file)`,
`name "tickets"`, `words "Rome", "tickets" in one passage`, `passage keyword "Rome", from "reserva en Roma"`,
`type label "ticket" (automatic)`. Which record field gave an about key is recomputed here (`about_sources`), never
stored. Mentioned and By-translation cards carry a passage cut WINDOW_PAD characters around the matched words or
the evidence span, at most WINDOW_MAX.

Public interface:
    explain(match, record, text, items) -> Explained        (sources, spans in the chunk text)
    not_matched(match) -> (str, ...)                          the words the file may miss, as the card names them
    passage_window(text, spans, limit) -> str                 a one-line cut of the chunk text around the spans
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .drive_enrichment import HintItem
from .drive_index_params import WINDOW_MAX, WINDOW_PAD
from .drive_key_sources import AboutField, AboutSource, KeyedRecord
from .drive_keys import about_sources, family_keys, match_spans, name_keys_v2, token_spans, word_form_keys
from .drive_match import SourceClass
from .drive_name_keys import _is_cyrillic
from .drive_search_texts import (
    MATCH_NAME, MATCH_PASSAGE_KEYWORD, MATCH_PLACE_IN_FILE, MATCH_TYPE_LABEL, MATCH_WORDS_IN_PASSAGE,
    NOT_MATCHED_COMMON,
)
from .drive_tiers import MISSED_COMMON, WHERE_CHUNK, WHERE_HINT, WHERE_NAME, Cover, FileMatch

Span = Tuple[int, int]
_ACROSS = ", by spelling in another script (automatic)"
_OTHER_SPELLING = ", by another spelling (automatic)"     # a skeleton match within one script (Харьков / Харків)
_LATIN_SKELETONS = ("xl:", "exl:")
_SPACES = re.compile(r"\s+")
_ELLIPSIS = "…"

# P6's match sources (§25.13.6 leaves them to P6, in the frozen ones' style)
_MATCH_WORD_IN_PASSAGE = "word {words} in one passage"
_MATCH_ENTITY = '{type} "{word}" (named in the file)'
# an entity spelling the profiler wrote that the file does not print (`ef:`): the file's own spelling beside it (xc F1)
_MATCH_ENTITY_FORM = '{type} "{word}" (spelling by the index; named in the file as "{printed}")'
_MATCH_PHOTO_PLACE = 'place "{word}" (photo location)'
_MATCH_VALUE = '{label} "{word}"'
_MATCH_DATE = 'date "{word}" ({label})'
_MATCH_FILE_DATE = 'date "{word}"'
_MATCH_KIND = 'kind "{word}"'
_MATCH_CAPTURE = 'capture "{word}"'          # §26.3: detected by code (`cp:`); a model's (`cpa:`) is "(automatic)"
_MATCH_AUTOMATIC = '{field} "{word}" (automatic)'
# the owner's note (DRIVE_FILE_NOTES_RFC §6.1): his own words, exact; a skeleton match adds the script note
_MATCH_NOTE_WORD = 'owner\'s note "{word}"'
_MATCH_NOTE_TYPE = 'owner\'s note: type "{word}"'
_MATCH_NOTE_AREA = 'owner\'s note: life area "{word}"'
_NOT_MATCHED = "not matched"
_AUTOMATIC_FIELD = {
    AboutField.TOPIC_WORD: "topic", AboutField.DOMAIN_WORD: "life area", AboutField.VALIDITY_WORD: "validity",
    AboutField.PHOTO_OBJECT: "photo object", AboutField.VISIBLE_TEXT_NUMBER: "text in the photo",
    AboutField.SCENE_LABEL: "scene",
}
_FAMILY_OF_FIELD = {
    AboutField.LABEL_WORD: "lw:", AboutField.TOPIC_WORD: "tw:", AboutField.DOMAIN_WORD: "dw:",
    AboutField.VALIDITY_WORD: "lv:", AboutField.PHOTO_WORD: "pw:",
    AboutField.SCENE_LABEL: "sl:",
}


@dataclass(frozen=True)
class Explained:
    sources: Tuple[str, ...]
    spans: Tuple[Span, ...]          # the matched words' or evidence spans in the chunk text (the passage window)


def _quoted(words: Sequence[str]) -> str:
    return ", ".join(f'"{w}"' for w in words)


def _name_word(name: str, keys: Sequence[str]) -> str:
    wanted = set(keys)
    for token, _, _ in token_spans(name):
        if wanted & set(name_keys_v2(token)):
            return token
    return ""


def _word_in(text: str, prefix: str, keys: Sequence[str]) -> str:
    """The token of a multi-word field (a description, a label phrase) whose `prefix` keys matched."""
    wanted = set(keys)
    for token, _, _ in token_spans(text):
        if wanted & set(family_keys(prefix, [token])):
            return token
    return text


def _about_text(source: AboutSource, keys: Sequence[str]) -> str:
    field, printed = source.field, source.printed
    if field is AboutField.OWNER_NOTE:
        template = {"type": _MATCH_NOTE_TYPE, "area": _MATCH_NOTE_AREA}.get(source.detail, _MATCH_NOTE_WORD)
        return template.format(word=printed)
    if field is AboutField.ENTITY_FORM:
        kind = source.detail if source.detail and source.detail != "other" else "entity"
        return _MATCH_ENTITY_FORM.format(type=kind, word=printed, printed=source.in_file)
    if field is AboutField.ENTITY:
        kind = source.detail if source.detail and source.detail != "other" else "entity"
        return (MATCH_PLACE_IN_FILE.format(word=printed) if kind == "place"
                else _MATCH_ENTITY.format(type=kind, word=printed))
    if field is AboutField.PLACE:
        return _MATCH_PHOTO_PLACE.format(word=printed)
    if field in (AboutField.VALUE, AboutField.DATE) and source.source_class is SourceClass.AUTOMATIC_RECORD:
        # a value or date the file's text does not hold (`va:`/`ya:`/`ma:`, Δ60): the model's, never shown as the file's
        return _MATCH_AUTOMATIC.format(field=source.detail.replace("_", " ") or field.value, word=printed)
    if field is AboutField.VALUE:
        return _MATCH_VALUE.format(label=source.detail.replace("_", " ") or "value", word=printed)
    if field is AboutField.DATE:
        return _MATCH_DATE.format(word=printed, label=source.detail.replace("_", " "))
    if field is AboutField.EFFECTIVE_DATE:
        return _MATCH_FILE_DATE.format(word=printed)
    if field is AboutField.KIND:
        return _MATCH_KIND.format(word=printed)
    if field is AboutField.CAPTURE:
        if source.source_class is SourceClass.AUTOMATIC_RECORD:
            return _MATCH_AUTOMATIC.format(field="capture", word=printed)
        return _MATCH_CAPTURE.format(word=printed)
    if field is AboutField.LABEL_WORD:
        return MATCH_TYPE_LABEL.format(word=_word_in(printed, "lw:", keys) if " " in printed else printed)
    if field is AboutField.PHOTO_WORD:
        return _MATCH_AUTOMATIC.format(field=f"photo {source.detail.replace('_', ' ')}",
                                       word=_word_in(printed, "pw:", keys))
    word = printed
    prefix = _FAMILY_OF_FIELD.get(field)
    if prefix and " " in printed:
        word = _word_in(printed, prefix, keys)
    return _MATCH_AUTOMATIC.format(field=_AUTOMATIC_FIELD.get(field, field.value.replace("_", " ")), word=word)


def _record_source(cover: Cover, record: KeyedRecord) -> str:
    """The first field that gave one of the cover's keys; a source whose shown word is the query word as typed wins
    (a label list holds "медицинская запись" and "медицинские записи": the second is shown for "медицинские")."""
    sources = about_sources(record)
    found = [(_about_text(s, cover.keys), s) for key in cover.keys for s in sources.get(key, ())
             if s.source_class is cover.source_class]
    if not found:
        return f'"{cover.word}"'
    text, source = next(((t, s) for t, s in found if f'"{cover.word}"' in t.casefold()), found[0])
    if cover.source_class is SourceClass.TRANSLATION and source.field is not AboutField.ENTITY_FORM:
        return text + _script_note(cover)
    return text


def _script_note(cover: Cover) -> str:
    """A skeleton match's label: across scripts when the query word and the key's word are in different scripts
    ("Berlin" / "Берлине"), another spelling within one ("Харьков" / "Харків"). A Latin query word never reads a
    Latin word's skeleton (xc F3), so a Latin query always crosses."""
    latin_query = not _is_cyrillic(cover.word)
    latin_key = any(k.startswith(_LATIN_SKELETONS) for k in cover.keys)
    return _ACROSS if latin_query or latin_key else _OTHER_SPELLING


def _hint_item(cover: Cover, items: Sequence[HintItem]) -> Optional[HintItem]:
    wanted = set(cover.keys)
    return next((i for i in items if i.evidence_start >= 0 and wanted & set(word_form_keys(i.text))), None)


def explain(match: FileMatch, record: KeyedRecord, name: str, text: Optional[str],
            items: Sequence[HintItem]) -> Explained:
    """The card's match sources in query order (passage words joined into one "in one passage" source) and the spans
    the passage window is cut around."""
    sources: List[str] = []
    spans: List[Span] = []
    passage_words: List[str] = []
    passage_at: Optional[int] = None
    for cover in match.covers:
        if cover.where == WHERE_NAME:
            word = _name_word(name, cover.keys) or cover.word
            sources.append(MATCH_NAME.format(word=word) + (_script_note(cover)
                                                           if cover.source_class is SourceClass.TRANSLATION else ""))
        elif cover.where == WHERE_CHUNK:
            found = match_spans(text or "", cover.keys)
            printed = (text or "")[found[0][0]:found[0][1]] if found else cover.word
            spans.extend(found[:1])
            if cover.source_class is SourceClass.TRANSLATION:
                sources.append(_MATCH_WORD_IN_PASSAGE.format(words=_quoted([printed])) + _script_note(cover))
            else:
                if passage_at is None:
                    passage_at = len(sources)
                    sources.append("")
                passage_words.append(printed)
        elif cover.where == WHERE_HINT:
            item = _hint_item(cover, items)
            if item is None:
                sources.append(MATCH_PASSAGE_KEYWORD.format(word=cover.word, evidence="").replace(', from ""', ""))
            else:
                sources.append(MATCH_PASSAGE_KEYWORD.format(word=item.text, evidence=item.evidence))
                spans.append((item.evidence_start, item.evidence_end))
        else:
            sources.append(_record_source(cover, record))
    if passage_at is not None:
        template = MATCH_WORDS_IN_PASSAGE if len(passage_words) > 1 else _MATCH_WORD_IN_PASSAGE
        sources[passage_at] = template.format(words=_quoted(passage_words))
    return Explained(tuple(sources), tuple(sorted(spans)))


def not_matched(match: FileMatch) -> Tuple[str, ...]:
    """`not matched (common): "photo"`, `not matched: "tickets"` (the low-weight allowance)."""
    out: Dict[str, List[str]] = {}
    for word, reason in match.missed:
        label = NOT_MATCHED_COMMON if reason == MISSED_COMMON else _NOT_MATCHED
        out.setdefault(label, []).append(word)
    return tuple(f"{label}: {_quoted(words)}" for label, words in out.items())


def passage_window(text: str, spans: Sequence[Span], limit: int = WINDOW_MAX) -> str:
    """The chunk text from WINDOW_PAD before the first span to WINDOW_PAD after the last, at most `limit` characters
    (centred on the spans when they do not fit), whitespace collapsed to one line. Empty without spans."""
    if not spans or limit <= 0 or not text:
        return ""
    first = min(s for s, _ in spans)
    last = max(e for _, e in spans)
    start, end = max(0, first - WINDOW_PAD), min(len(text), last + WINDOW_PAD)
    if end - start > limit:
        middle = (first + last) // 2
        start = max(0, min(middle - limit // 2, len(text) - limit))
        end = min(len(text), start + limit)
    return _SPACES.sub(" ", text[start:end]).strip()
