"""The fact in the owner's message language: what the review pass translates and what the owner is shown.

Stored facts are written in English. A review message in another language quotes the fact translated by the review
pass, which is presentation only: the fact, its id and its `quoted_text` stay as stored. A translation is keyed by the
stored text (`quote_digest`), not by a fact document, and is shown only while it still passes `translation_problem`.
Anything that does not pass is not shown: the owner sees the stored text."""
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import timedelta
from typing import Final, List, Mapping, Optional, Tuple

SOURCE_LANGUAGE: Final[str] = "en"  # the language stored facts are written in
MAX_TRANSLATION_CHARS: Final[int] = 500  # a fact is at most 40 words; a translation longer than this is not one
QUOTE_TRANSLATION_RETENTION: Final[timedelta] = timedelta(days=180)
URL_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?i)[a-z][a-z0-9+.\-]*://|www\.")
# `[` would imitate a history frame (`[System:`, `[Reply to memory review`).
FRAME_CHARS: Final[frozenset[str]] = frozenset("[")
INVISIBLE_CATEGORIES: Final[frozenset[str]] = frozenset({"Cc", "Cf"})  # control and format characters (bidi, zero-width)
_CODE = re.compile(r"[a-z]{2}")
_DIGEST = re.compile(r"[0-9a-f]{16}")
_DIGIT_RUN = re.compile(r"[0-9]+")
# The text key of a rewrite's content among the texts a pass writes (P2-3); an ask's texts go by their own keys.
HISTORY_TEXT_KEY: Final[str] = "history"


@dataclass(frozen=True)
class QuoteTranslation:
    """A stored fact's text as the review shows it in `language` (ISO 639-1): `text`, written by the review pass for the
    English text whose `quote_digest` is `source_digest`. Keyed by that text, not by a fact document. Presentation only."""

    language: str
    source_digest: str
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.language, str) or not _CODE.fullmatch(self.language):
            raise ValueError("a quote translation's language is two lowercase ASCII letters")
        if not isinstance(self.source_digest, str) or not _DIGEST.fullmatch(self.source_digest):
            raise ValueError("a quote translation's source digest is 16 lowercase hex characters")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("a quote translation's text is not empty")


def quote_digest(text: str) -> str:
    """sha256 hex, first 16, of the text with its whitespace collapsed and NFC-normalised."""
    normal = unicodedata.normalize("NFC", " ".join(text.split()))
    return hashlib.sha256(normal.encode("utf-8")).hexdigest()[:16]


def _digit_runs(text: str) -> List[str]:
    return sorted(_DIGIT_RUN.findall(text))


def _carries_frame_or_link(text: str) -> bool:
    return any(ch in text for ch in FRAME_CHARS) or URL_PATTERN.search(text) is not None


def translation_problem(value: object, source: str) -> Optional[str]:
    """Why `value` may not be shown as the translation of `source`, or None.

    Refused: not a non-empty text; more than one line; a control or format character; `[`; a URL; longer than
    `MAX_TRANSLATION_CHARS`; a different multiset of ASCII digit runs than the source's (a number must survive the
    translation); a source that itself has a URL or `[` (such a fact is always shown as stored)."""
    if _carries_frame_or_link(source):
        return "the source has a link or ["
    if not isinstance(value, str) or not value.strip():
        return "not a text"
    text = value.strip()
    if len(text.splitlines()) != 1:
        return "more than one line"
    if any(unicodedata.category(ch) in INVISIBLE_CATEGORIES for ch in text):
        return "has a control or format character"
    if any(ch in text for ch in FRAME_CHARS):
        return "has ["
    if URL_PATTERN.search(text):
        return "has a URL"
    if len(text) > MAX_TRANSLATION_CHARS:
        return "too long"
    if _digit_runs(text) != _digit_runs(source):
        return "the numbers differ from the source"
    return None


def written_text_key(fact_id: str, text_key: str) -> str:
    """The `translations` key of a text the pass wrote about the batch fact `fact_id`: `<fact_id>:<text key>`."""
    return f"{fact_id}:{text_key}"


def translations_wanted(language: Optional[str]) -> bool:
    """Whether a message in `language` can show translations: a two-letter code other than `SOURCE_LANGUAGE`."""
    return isinstance(language, str) and _CODE.fullmatch(language) is not None and language != SOURCE_LANGUAGE


def report_translations(
    raw: object, language: Optional[str], sources: Mapping[str, str],
) -> Tuple[Tuple[QuoteTranslation, ...], List[str]]:
    """The translations a final report gave (`raw` = its `translations` value). `sources` maps each key to its English
    text: a batch fact id to its stored text, and a `written_text_key` to a text the pass wrote (P2-3). Returns
    `(kept, refusals)`.

    Nothing is kept and nothing refused when `translations_wanted(language)` is false or `raw` is None. Refusals are
    `"<key>: <why>"` or one `"translations: <why>"`, never a text: `raw` not an object; a key not in `sources`; a
    value with a `translation_problem`. A kept one is stripped and keyed by `quote_digest` of the source text; two
    sources with the same text give one record (the later wins). Order: the order of `sources`."""
    if raw is None or language is None or not translations_wanted(language):
        return (), []
    if not isinstance(raw, Mapping):
        return (), ["translations: not an object"]
    refusals: List[str] = []
    for key in raw:
        if key not in sources:
            refusals.append(f"{key}: names no listed fact or text this pass wrote")
    kept: dict[str, QuoteTranslation] = {}
    for key, source in sources.items():
        if key not in raw:
            continue
        value = raw[key]
        problem = translation_problem(value, source)
        if problem is not None:
            refusals.append(f"{key}: {problem}")
            continue
        digest = quote_digest(source)
        kept.pop(digest, None)  # the later wins, at the later position
        kept[digest] = QuoteTranslation(language, digest, str(value).strip())
    return tuple(kept.values()), refusals


def shown_quote(english: str, translation: Optional[QuoteTranslation], language: Optional[str]) -> str:
    """The fact as the owner is shown it: `translation.text` when there is one, in `language`, and it passes
    `translation_problem` against `english` now; else `english`. (The lookup by language and `quote_digest(english)` is
    what matches the text; no separate digest comparison here.)"""
    if translation is None or language is None or translation.language != language:
        return english
    if translation_problem(translation.text, english) is not None:
        return english
    return translation.text
