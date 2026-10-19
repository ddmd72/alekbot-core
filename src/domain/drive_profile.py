"""
Drive profile — the per-kind profile policy, the profiler's input from stored text, and the verification of its output
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §25.3, §25.4.1). Pure: no I/O, no clock.

Public interface:
    InputRule, ProfileOutput, TemplateReason                     (enums)
    KindPolicy(input_rule, output, doc2query)
    PROFILE_POLICY: kind -> KindPolicy                           the §25.3 table (photographs: triage, not here)
    TEMPLATE_POLICY, NO_POLICY
    policy_of(kind, *, text_chars, triaged_text=False, machine_text=False, script_rendered=False, readme=False)
    record_policy(rec, *, text_chars, script_rendered=False) -> KindPolicy
    stored_text(chunks) -> str                                   the reassembled stored text
    profile_input(record, chunks, kind) -> Optional[ProfileInput]   None: the policy sends no model input
    input_fingerprint(kind, inp) -> str                          `profile_input_fingerprint` (not the name)
    readme_excerpt(readme_text, child_names) -> str              a collapsed folder's `readme_excerpt`
    template_profile(reason, kind, text) -> DriveProfile         `profile_source = template`
    VerifyDrops; verify(profile, chunks, name, *, key_values=True) -> (DriveProfile, VerifyDrops)
    valid_until_of(profile), issued_of(profile) -> Optional[date]

Every input is cut from the STORED text (§25.4.1): chunks in index order, each contributing the last
`char_end - char_start` characters of its text (a continued table chunk starts with a repeated header outside its
span), overlaps cut by offsets, gaps as a paragraph break. "Head" and "tail" end and start at a paragraph or sentence
boundary. The file's name and the previous labels travel on the request, outside the fingerprint (D21, N13): the
profile comes only from the file's own content.

Verification (P5) runs over the whole stored text: an entity is kept only when its `printed` occurs in the text or the
name; a key value only when one of its number terms (`drive_content_terms`, the `n:` normaliser) is among the text's,
or its `printed` occurs verbatim, and its `canonical` becomes that normaliser form (the `v:` key the query meets); a
date only when its ISO form is among the text's number terms. Labels outside `drive_labels` become `other` (dates,
entity types, the document type), `other_id` (value labels) or are dropped (life domains). Descriptive fields and the
word lists are not verifiable: automatic (an entry joined with commas is split into its words). Matching is NFC +
casefold + whitespace collapse, on word boundaries.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import date
from enum import Enum
from typing import Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Set, Tuple

from .drive_content_terms import content_terms, query_terms
from .drive_enrichment import DriveProfile, KeyValue, ProfileDate, ProfileEntity
from .drive_enrichment_calls import ProfileInput
from .drive_index import PROFILE_VERSION, DriveIndexRecord, Reason, TriageVerdict
from .drive_index_params import (
    ENTITY_FORMS_MAX, PROFILE_DATES_MAX, PROFILE_ENTITIES_MAX, PROFILE_EPUB_BODY_CHARS, PROFILE_HEAD_CHARS,
    PROFILE_HEADINGS_CHARS, PROFILE_HEADINGS_MIN_TEXT, PROFILE_KEY_VALUES_MAX, PROFILE_MIN_CHARS, PROFILE_SHEET_ROWS,
    PROFILE_TAIL_CHARS, PROFILE_TOPICS_MAX, PROFILE_WORD_LIST_MAX, README_CHILD_NAMES, README_EXCERPT_HEAD_CHARS,
    README_EXCERPT_TAIL_CHARS,
)
from .drive_labels import DATE_LABELS, DOC_TYPES, ENTITY_TYPES, LIFE_DOMAINS, VALUE_LABELS


class InputRule(str, Enum):
    """How the profiler's input is cut from the stored text (§25.3)."""
    DOCUMENT = "document"     # head 6 000 + tail 1 500; heading lines over 20 000 chars
    SLIDES = "slides"         # slide titles; head 6 000; tail 1 500
    SHEETS = "sheets"         # sheet names, header rows, first 20 rows per sheet (<= 6 000)
    JSON = "json"             # head 6 000
    EPUB = "epub"             # metadata lines and the first 3 000 chars of the body
    EMAIL = "email"           # headers + head 6 000 + tail 1 500 of the body
    CUES = "cues"             # head 6 000 + tail 1 500 of the cue text
    README = "readme"         # a collapsed folder's readme_excerpt
    NONE = "none"


class ProfileOutput(str, Enum):
    FULL = "full"
    NO_KEY_VALUES = "no_key_values"
    TEMPLATE = "template"     # no model call (§25.3)
    NONE = "none"             # no profile at all


class TemplateReason(str, Enum):
    SHORT = "short"                       # a note under PROFILE_MIN_CHARS (§25.3)
    SCRIPT_RENDERED = "script_rendered"   # parser@2's flag (§25.3)
    MACHINE_TEXT = "machine_text"         # a machine_text demotion


@dataclass(frozen=True)
class KindPolicy:
    input_rule: InputRule
    output: ProfileOutput
    doc2query: bool


_DOC = KindPolicy(InputRule.DOCUMENT, ProfileOutput.FULL, True)
_SLIDES = KindPolicy(InputRule.SLIDES, ProfileOutput.FULL, True)
_SHEETS = KindPolicy(InputRule.SHEETS, ProfileOutput.NO_KEY_VALUES, False)
_EMAIL = KindPolicy(InputRule.EMAIL, ProfileOutput.FULL, True)

PROFILE_POLICY: Mapping[str, KindPolicy] = {
    **{kind: _DOC for kind in ("pdf", "docx", "doc", "odt", "rtf", "html", "mhtml", "markdown", "text")},
    **{kind: _SLIDES for kind in ("pptx", "ppt", "odp")},
    **{kind: _SHEETS for kind in ("xlsx", "xls", "ods", "csv")},
    "json": KindPolicy(InputRule.JSON, ProfileOutput.NO_KEY_VALUES, False),
    "epub": KindPolicy(InputRule.EPUB, ProfileOutput.NO_KEY_VALUES, False),
    "eml": _EMAIL,
    "msg": _EMAIL,
    "subtitles": KindPolicy(InputRule.CUES, ProfileOutput.FULL, True),
    "folder": KindPolicy(InputRule.README, ProfileOutput.NO_KEY_VALUES, False),   # a collapsed folder's README
}
TEMPLATE_POLICY = KindPolicy(InputRule.NONE, ProfileOutput.TEMPLATE, False)
NO_POLICY = KindPolicy(InputRule.NONE, ProfileOutput.NONE, False)

# Bounds on descriptive fields that land in a card (§25.2.7), not repairs: longer values are cut, never rewritten.
TITLE_MAX_CHARS = 200
WHAT_IT_IS_MAX_CHARS = 300
_TEMPLATE_MACHINE_TEXT = "machine-generated text"
_README_NAMES_MARK = "\n\n<<folder contents>>\n"
_NOTE_KINDS = frozenset({"markdown", "text"})
LIFE_DOMAINS_MAX = 3            # "one to three life areas" (§25.4.1)
_README_TAIL_MARK = "\n\n<<…>>\n\n"


def policy_of(kind: str, *, text_chars: int, triaged_text: bool = False, machine_text: bool = False,
              script_rendered: bool = False, readme: bool = False) -> KindPolicy:
    """The policy for one file. `text_chars`: its stored text length; `triaged_text`: an image triaged as text and read
    by the reader (a scan: profiled as a document); `readme`: a collapsed folder with a README excerpt. A note
    (Markdown, plain text) under PROFILE_MIN_CHARS, a script-rendered page and a machine-text demotion get a template,
    no model call; a short document of any other kind (a receipt, a ticket) is profiled (§25.3, review m4)."""
    if kind == "folder":
        return PROFILE_POLICY["folder"] if readme else NO_POLICY
    policy = _DOC if kind == "image" and triaged_text else PROFILE_POLICY.get(kind, NO_POLICY)
    if policy is NO_POLICY:
        return NO_POLICY
    if machine_text or script_rendered or (kind in _NOTE_KINDS and text_chars < PROFILE_MIN_CHARS):
        return TEMPLATE_POLICY
    return policy


def record_policy(rec: DriveIndexRecord, *, text_chars: int, script_rendered: bool = False) -> KindPolicy:
    """`policy_of` with the record's own facts (kind, triage verdict, machine-text demotion, README excerpt).
    `script_rendered` comes from the extraction outcome (parser@2), which the record does not keep."""
    return policy_of(rec.kind, text_chars=text_chars, triaged_text=rec.triage_verdict is TriageVerdict.TEXT,
                     machine_text=rec.reason is Reason.MACHINE_TEXT, script_rendered=script_rendered,
                     readme=bool(rec.readme_excerpt))


class _HasSpan(Protocol):
    @property
    def index(self) -> int: ...
    @property
    def text(self) -> str: ...
    @property
    def char_start(self) -> int: ...
    @property
    def char_end(self) -> int: ...


class _HasText(Protocol):
    @property
    def text(self) -> str: ...


def stored_text(chunks: Sequence[_HasSpan]) -> str:
    """The stored text reassembled from its chunks (§25.4.1)."""
    out: List[str] = []
    end = None
    for chunk in sorted(chunks, key=lambda c: c.index):
        span = max(0, chunk.char_end - chunk.char_start)
        own = chunk.text[len(chunk.text) - span:] if span else ""
        if end is not None and chunk.char_start < end:
            own = own[end - chunk.char_start:]           # the overlap the previous chunk already gave
        elif end is not None and chunk.char_start > end:
            out.append("\n\n")                           # a gap
        out.append(own)
        end = max(end or 0, chunk.char_end)
    return "".join(out)


def profile_input(record: DriveIndexRecord, chunks: Sequence[_HasSpan], kind: str) -> Optional[ProfileInput]:
    """The profiler's input under `kind`'s rule (§25.3), or None when the record's policy sends nothing to a model
    (no profile, a template, nothing readable). `script_rendered` pages are the caller's to template (the flag lives
    on the extraction outcome)."""
    if kind == "folder":
        if not record.readme_excerpt:
            return None
        text, _, names = record.readme_excerpt.partition(_README_NAMES_MARK)
        head, _, tail = text.partition(_README_TAIL_MARK)
        return ProfileInput(head=head, tail=tail,
                            child_names=tuple(n for n in names.split("\n") if n)[:README_CHILD_NAMES])
    text = stored_text(chunks)
    if not text.strip():
        return None                           # nothing readable
    policy = record_policy(replace(record, kind=kind), text_chars=len(text.strip()))
    rule = policy.input_rule
    if policy.output in (ProfileOutput.NONE, ProfileOutput.TEMPLATE) or rule is InputRule.NONE:
        return None
    if rule is InputRule.SHEETS:
        table_head, sheet_names = _sheets(text)
        return ProfileInput(head=table_head, headings=sheet_names)
    if rule is InputRule.JSON:
        return ProfileInput(head=_cut_head(text, PROFILE_HEAD_CHARS))
    if rule is InputRule.EPUB:
        meta, body = _epub_parts(text)
        body_head = _cut_head(body, PROFILE_EPUB_BODY_CHARS)
        return ProfileInput(head=f"{meta}\n\n{body_head}" if meta else body_head)
    if rule is InputRule.EMAIL:
        headers, sep, body = text.partition("\n\n")
        if not sep:
            headers, body = "", text
        head, tail = _head_tail(body, PROFILE_HEAD_CHARS, PROFILE_TAIL_CHARS)
        return ProfileInput(head=f"{headers}\n\n{head}" if headers else head, tail=tail)
    head, tail = _head_tail(text, PROFILE_HEAD_CHARS, PROFILE_TAIL_CHARS)
    if rule is InputRule.SLIDES or len(text) > PROFILE_HEADINGS_MIN_TEXT:
        return ProfileInput(head=head, tail=tail, headings=_headings(text))
    return ProfileInput(head=head, tail=tail)


def input_fingerprint(kind: str, inp: ProfileInput) -> str:
    """`profile_input_fingerprint` (§25.2.1): kind, the input's slices and PROFILE_VERSION; not the name, not the
    previous labels. The planner compares it with `profile_fingerprint` without reading the text."""
    h = hashlib.sha256()
    for part in (f"v{PROFILE_VERSION}", kind, inp.head, inp.tail, *inp.headings, "\x1e", *inp.child_names):
        h.update(part.encode("utf-8", errors="replace"))
        h.update(b"\x1f")
    return h.hexdigest()[:32]


def readme_excerpt(readme_text: str, child_names: Sequence[str]) -> str:
    """`readme_excerpt` (§25.2.1, §25.3): the README's head and tail and at most README_CHILD_NAMES child names, in
    the form `profile_input` reads back."""
    head, tail = _head_tail(readme_text, README_EXCERPT_HEAD_CHARS, README_EXCERPT_TAIL_CHARS)
    text = f"{head}{_README_TAIL_MARK}{tail}" if tail else head
    names = [" ".join(n.split()) for n in child_names if n.strip()][:README_CHILD_NAMES]
    return text + (_README_NAMES_MARK + "\n".join(names) if names else "")


def template_profile(reason: TemplateReason, kind: str, text: str) -> DriveProfile:
    """The `template` profile (§25.3): a short note's text as `what_it_is` (`notes`), a script-rendered page
    (`web_page`), machine text ("machine-generated text"). No model call; nothing to verify."""
    if reason is TemplateReason.SCRIPT_RENDERED:
        return DriveProfile(title="", what_it_is="", language="", doc_type="web_page")
    if reason is TemplateReason.MACHINE_TEXT:
        return DriveProfile(title="", what_it_is=_TEMPLATE_MACHINE_TEXT, language="", doc_type="other")
    note = kind in ("markdown", "text")
    return DriveProfile(title="", what_it_is=" ".join(text.split())[:WHAT_IT_IS_MAX_CHARS], language="",
                        doc_type="notes" if note else "other")


# -- verification ---------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class VerifyDrops:
    """What verification removed or relabelled (telemetry `verify_dropped_*`, §25.7)."""
    entities: int = 0
    values: int = 0
    dates: int = 0
    labels: int = 0          # enum values outside the fixed set, relabelled or dropped


def verify(profile: DriveProfile, chunks: Sequence[_HasText], name: str, *,
           key_values: bool = True) -> Tuple[DriveProfile, VerifyDrops]:
    """The profile with only what the stored text (the chunks' texts) and the name support (§25.4.1), within the
    shared caps. `key_values=False` for kinds whose profile has none (§25.3). Dropping everything is not a
    failure."""
    texts = [c.text for c in chunks]
    haystack = _norm("\n".join(texts))
    name_norm = _norm(name)
    terms: Set[str] = set()
    for text in texts:
        terms.update(content_terms(text))
    labels = 0

    doc_type = profile.doc_type if profile.doc_type in DOC_TYPES else "other"
    labels += doc_type != profile.doc_type
    domains = _unique(d for d in profile.life_domains if d in LIFE_DOMAINS)[:LIFE_DOMAINS_MAX]
    labels += sum(1 for d in profile.life_domains if d not in LIFE_DOMAINS)

    entities: List[ProfileEntity] = []
    dropped_entities = 0
    for entity in profile.entities:
        if not entity.printed.strip() or not (_occurs(entity.printed, haystack) or _occurs(entity.printed,
                                                                                          name_norm)):
            dropped_entities += 1
            continue
        etype = entity.type if entity.type in ENTITY_TYPES else "other"
        labels += etype != entity.type
        forms = _unique(f.strip() for f in entity.forms)[:ENTITY_FORMS_MAX]
        name = entity.name.strip() or entity.printed.strip()
        # which of the model's own spellings the file (or its name) prints: only those are exact keys (xc F1)
        found = tuple(s for s in _unique((name, *forms))
                      if s != entity.printed.strip() and (_occurs(s, haystack) or _occurs(s, name_norm)))
        entities.append(ProfileEntity(name=name, type=etype, printed=entity.printed.strip(), forms=forms,
                                      found=found))

    values: List[KeyValue] = []
    dropped_values = 0
    for kv in profile.key_values:
        canonical = _verified_value(kv, terms, haystack) if key_values else None
        if canonical is None:
            dropped_values += 1
            continue
        label = kv.label if kv.label in VALUE_LABELS else "other_id"
        labels += label != kv.label
        values.append(KeyValue(label=label, printed=kv.printed.strip(), canonical=canonical))

    dates: List[ProfileDate] = []
    dropped_dates = 0
    for d in profile.dates:
        if d.iso.isoformat() not in terms:
            dropped_dates += 1
            continue
        label = d.label if d.label in DATE_LABELS else "other"
        labels += label != d.label
        dates.append(ProfileDate(label=label, iso=d.iso, printed=d.printed))
    dates = dates[:PROFILE_DATES_MAX]

    verified = DriveProfile(
        title=" ".join(profile.title.split())[:TITLE_MAX_CHARS],
        what_it_is=" ".join(profile.what_it_is.split())[:WHAT_IT_IS_MAX_CHARS],
        language=profile.language.strip()[:16],
        doc_type=doc_type,
        life_domains=domains,
        dates=tuple(dates),
        entities=tuple(entities)[:PROFILE_ENTITIES_MAX],
        key_values=tuple(values)[:PROFILE_KEY_VALUES_MAX],
        topics=_words(profile.topics, PROFILE_TOPICS_MAX),
        topics_native=_words(profile.topics_native, PROFILE_TOPICS_MAX),
        topic_words=_words(_split(profile.topic_words), PROFILE_WORD_LIST_MAX),
        label_words=_words(_split(profile.label_words), PROFILE_WORD_LIST_MAX),
        domain_words=_words(_split(profile.domain_words), PROFILE_WORD_LIST_MAX),
        validity_words=(),
    )
    if _validity_date(verified) is not None:
        verified = replace(verified, validity_words=_words(_split(profile.validity_words), PROFILE_WORD_LIST_MAX))
    return verified, VerifyDrops(entities=dropped_entities, values=dropped_values, dates=dropped_dates,
                                 labels=labels)


def valid_until_of(profile: DriveProfile) -> Optional[date]:
    """`valid_until` (§25.2.1): the verified `valid_until` date, else the verified `due` date."""
    return _validity_date(profile)


def issued_of(profile: DriveProfile) -> Optional[date]:
    """The verified `issued` date: the profiled document's `effective_date` (D13 extended, §25.4.1)."""
    return next((d.iso for d in profile.dates if d.label == "issued"), None)


# -- implementation -------------------------------------------------------------------------------------------------

_SENTENCE_END = re.compile(r"[.!?…:;](?=\s)|\n")
_HEADING = re.compile(r"^#{1,6}\s+\S")
_TABLE_ROW = re.compile(r"^\|.*\|\s*$")
_TABLE_RULE = re.compile(r"^\|(?:\s*:?-{2,}:?\s*\|)+\s*$")
_EPUB_META = re.compile(r"^\*\*[^*\n]{1,40}:\*\*")


def _cut_head(text: str, limit: int) -> str:
    """At most `limit` chars from the start, ending at a paragraph boundary (when that keeps half), else a sentence or
    line boundary, else a word boundary."""
    if len(text) <= limit:
        return text.strip()
    piece = text[:limit]
    para = piece.rfind("\n\n")
    if para >= limit // 2:
        return piece[:para].rstrip()
    ends = [m.end() for m in _SENTENCE_END.finditer(piece)]
    if ends and ends[-1] >= limit // 2:
        return piece[:ends[-1]].rstrip()
    space = piece.rfind(" ")
    return (piece[:space] if space >= limit // 2 else piece).rstrip()


def _cut_tail(text: str, limit: int) -> str:
    """At most `limit` chars from the end, starting after a paragraph boundary (when that keeps half), else after a
    sentence or line boundary, else after a word boundary."""
    if len(text) <= limit:
        return text.strip()
    piece = text[len(text) - limit:]
    para = piece.find("\n\n")
    if 0 <= para <= limit // 2:
        return piece[para:].strip()
    match = _SENTENCE_END.search(piece)
    if match is not None and match.end() <= limit // 2:
        return piece[match.end():].strip()
    space = piece.find(" ")
    return (piece[space:] if 0 <= space <= limit // 2 else piece).strip()


def _head_tail(text: str, head_chars: int, tail_chars: int) -> Tuple[str, str]:
    if len(text) <= head_chars + tail_chars:
        return text.strip(), ""
    return _cut_head(text, head_chars), _cut_tail(text, tail_chars)


def _headings(text: str) -> Tuple[str, ...]:
    out: List[str] = []
    used = 0
    for line in text.splitlines():
        line = line.strip()
        if _HEADING.match(line):
            if used + len(line) + 1 > PROFILE_HEADINGS_CHARS:
                break
            out.append(line)
            used += len(line) + 1
    return tuple(out)


def _sheets(text: str) -> Tuple[str, Tuple[str, ...]]:
    """Sheet names, each table's header row (and rule) and its first PROFILE_SHEET_ROWS rows, at most
    PROFILE_HEAD_CHARS, cut at a line boundary."""
    lines: List[str] = []
    names: List[str] = []
    rows = 0
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            names.append(stripped[3:].strip())
            lines.append(stripped)
            rows = -1
            continue
        if _TABLE_ROW.match(stripped):
            if _TABLE_RULE.match(stripped):
                lines.append(stripped)
                continue
            if rows < 0 or not lines or not _TABLE_ROW.match(lines[-1]):
                rows = 0                       # a header row
                lines.append(stripped)
                continue
            rows += 1
            if rows <= PROFILE_SHEET_ROWS:
                lines.append(stripped)
            continue
        if stripped:
            lines.append(stripped)
    out: List[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > PROFILE_HEAD_CHARS:
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out), tuple(n for n in names if n)


def _epub_parts(text: str) -> Tuple[str, str]:
    lines = text.lstrip().split("\n")
    meta: List[str] = []
    while lines and _EPUB_META.match(lines[0]):
        meta.append(lines.pop(0))
    return "\n".join(meta), "\n".join(lines).strip()


def _norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).casefold().split())


def _occurs(printed: str, haystack: str) -> bool:
    needle = _norm(printed)
    return bool(needle) and re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None


def _verified_value(kv: KeyValue, terms: Set[str], haystack: str) -> Optional[str]:
    """The value's normaliser form when the maximal number term of its `printed`, else of its `canonical`, is the
    text's — never a fragment of it (review m7) — or when `printed` occurs verbatim (then its first number term, else
    its casefolded spelling without spaces); None otherwise."""
    candidates = [terms_[0] for terms_ in (query_terms(kv.printed), query_terms(kv.canonical)) if terms_]
    hit = next((t for t in candidates if t in terms), None)
    if hit is not None:
        return hit
    if kv.printed.strip() and _occurs(kv.printed, haystack):
        printed_terms = query_terms(kv.printed)
        return printed_terms[0] if printed_terms else "".join(_norm(kv.printed).split())
    return None


def _validity_date(profile: DriveProfile) -> Optional[date]:
    by_label: Dict[str, date] = {}
    for d in profile.dates:
        by_label.setdefault(d.label, d.iso)
    return by_label.get("valid_until") or by_label.get("due")


def _unique(items: Iterable[str]) -> Tuple[str, ...]:
    return tuple(dict.fromkeys(i for i in items if i))


def _split(items: Sequence[str]) -> List[str]:
    """Word-list entries, one word or phrase each: an entry the model joined with commas or semicolons is split (seen
    live: 'electricity, electric, luz' as one entry)."""
    return [part for item in items for part in re.split(r"[,;]", item)]


def _words(items: Sequence[str], cap: int) -> Tuple[str, ...]:
    return _unique(" ".join(i.split()) for i in items)[:cap]
