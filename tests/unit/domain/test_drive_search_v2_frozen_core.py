"""Search v2 frozen core (USER_DRIVE_INDEXING_RFC §25.13): the types, label sets, `k:` table, version constants,
parameters and verbatim texts the plan parts are written against. Pins the surface only — no behaviour."""
import re
import typing
from dataclasses import FrozenInstanceError, fields
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from src.domain import drive_index_params as P
from src.domain import drive_search_texts as T
from src.domain.drive_content_identity import ContentIdentity
from src.domain.drive_enrichment import (
    JOB_USAGE_CLASS, UNGROUNDED, USD_CAPS, DriveHintDoc, DriveProfile, EnrichContext, EnrichJob, HintItem, HintType,
    HoldReason, InitialBuild, JobState, KeyValue, ModelUsageMonth, PhotoDescription, PhotoObject, ProfileDate,
    ProfileEntity, ProfileSource, UsageClass,
)
from src.domain.drive_enrichment_calls import (
    HintReply, HintRequest, ModelReplyStatus, ProfileInput, ProfileLabels, ProfileReply, ProfileRequest, TriageReply,
    TriageResultV2,
)
from src.domain.drive_index import (
    CURRENT_VERSIONS, KEYS_VERSION, MIGRATION_GATE_KEY, PASSAGES_VERSION, PROFILE_VERSION, SEARCH_V2_VERSIONS,
    DriveChunk, DriveIndexRecord, DriveSyncState, RecordState, Scope, TriageVerdict,
)
from src.domain.drive_index_lean import DriveChunkLean, DriveHintLean, DriveRecordLean
from src.domain.drive_labels import (
    DATE_LABELS, DOC_TYPES, ENTITY_TYPES, KIND_WORDS, LIFE_DOMAINS, PHOTO_KIND_WORDS, VALUE_LABELS,
)
from src.domain.drive_match import FAMILY_SOURCE_CLASS, Concept, KeyFamily, MatchTier, QueryConcepts, SourceClass
from src.domain.drive_search import KIND_NAMES

NOW = datetime(2026, 10, 13, tzinfo=timezone.utc)


RFC = (Path(__file__).resolve().parents[3] / "docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md").read_text(encoding="utf-8")


def names(cls):
    return [f.name for f in fields(cls)]


def rfc_quote(after: str, until: str) -> str:
    """The RFC text between two markers, line wraps joined with a space (the RFC wraps at 120 columns)."""
    start = RFC.index(after) + len(after)
    return re.sub(r"\s*\n\s*", " ", RFC[start:RFC.index(until, start)])


def rfc_label_set(name: str) -> tuple:
    match = re.search(r"`" + name + r"`(?: \(\d+\))?: `([^`]+)`", RFC)
    assert match, name
    return tuple(w.strip() for w in re.sub(r"\s+", " ", match.group(1)).split(","))


class TestVersions:
    def test_v2_keys_are_defined(self):
        assert SEARCH_V2_VERSIONS == {"keys": KEYS_VERSION, "profile": PROFILE_VERSION, "passages": PASSAGES_VERSION}
        assert all(isinstance(v, int) and v >= 1 for v in SEARCH_V2_VERSIONS.values())
        assert MIGRATION_GATE_KEY == "accuracy"   # Δ65 (§26.7): the newest key a from-scratch onboarding stamps

    def test_v2_keys_are_current_behind_the_gate(self):
        """P5 added them to CURRENT_VERSIONS behind the migration gate (§25.8 as amended by D30): the gate key is one
        of them, so a v1-built state (which lacks it) is told apart from a v2 onboarding (which stamps all of them)."""
        assert {k: CURRENT_VERSIONS[k] for k in SEARCH_V2_VERSIONS} == dict(SEARCH_V2_VERSIONS)
        assert MIGRATION_GATE_KEY in CURRENT_VERSIONS


class TestLabels:
    def test_doc_types(self):
        assert DOC_TYPES == (
            "receipt", "invoice", "bill", "bank_statement", "payslip", "tax_document", "contract", "insurance_policy",
            "warranty", "ticket", "booking", "certificate", "id_document", "medical_record", "prescription", "fine",
            "letter", "form", "manual", "book", "article", "report", "plan", "notes", "recipe", "newsletter",
            "web_page", "spreadsheet", "data", "presentation", "other")
        assert len(DOC_TYPES) == len(set(DOC_TYPES)) == 31

    def test_life_domains(self):
        assert LIFE_DOMAINS == ("finance", "health", "travel", "home", "vehicle", "work", "education", "legal_admin",
                                "shopping", "leisure", "family", "tech", "news_reference", "other")
        assert len(set(LIFE_DOMAINS)) == 14

    def test_the_other_sets(self):
        assert DATE_LABELS == ("issued", "event", "valid_from", "valid_until", "due", "period_end", "other")
        assert ENTITY_TYPES == ("place", "organization", "person", "event", "product", "work", "other")
        assert VALUE_LABELS == (
            "order_number", "ticket_number", "booking_reference", "invoice_number", "receipt_number",
            "account_number", "iban", "card_number_masked", "policy_number", "contract_number", "customer_number",
            "reference", "tax_id", "document_number", "personal_id_number", "phone", "amount_total", "amount_due",
            "price", "other_id")

    @pytest.mark.parametrize("name, value", [("DOC_TYPES", DOC_TYPES), ("LIFE_DOMAINS", LIFE_DOMAINS),
                                             ("DATE_LABELS", DATE_LABELS), ("ENTITY_TYPES", ENTITY_TYPES),
                                             ("VALUE_LABELS", VALUE_LABELS)])
    def test_equal_to_the_rfc_text(self, name, value):
        assert value == rfc_label_set(name)

    def test_unstable_labels_stay_out(self):
        assert "personal_record" not in DOC_TYPES and "issuer" not in ENTITY_TYPES + VALUE_LABELS


class TestKindTable:
    @pytest.mark.parametrize("kinds, words", [
        (("pdf",), ("pdf",)),
        (("docx", "doc", "odt", "rtf"), ("document", "documento", "документ")),
        (("xlsx", "xls", "ods", "csv"), ("spreadsheet", "table", "hoja de cálculo", "tableur", "таблица", "таблиця")),
        (("pptx", "ppt", "odp"), ("presentation", "slides", "presentación", "présentation", "презентация",
                                  "презентація")),
        (("markdown", "text"), ("note", "text", "nota", "texte", "заметка", "нотатка")),
        (("html", "mhtml"), ("web page", "página web", "page web", "веб-страница", "веб-сторінка")),
        (("eml", "msg"), ("email", "mail", "correo", "courriel", "письмо", "лист")),
        (("epub",), ("book", "e-book", "libro", "livre", "книга")),
        (("video",), ("video", "vídeo", "vidéo", "видео", "відео")),
        (("audio",), ("audio", "recording", "grabación", "enregistrement", "аудио", "аудіо", "аудиозапись",
                      "аудіозапис")),
        (("archive",), ("archive", "zip", "archivo", "архив", "архів")),
        (("folder",), ("folder", "carpeta", "dossier", "папка")),
    ])
    def test_rows(self, kinds, words):
        """§25.13.5 as revised by the P1 review (M7 + French words, KEYS_VERSION 2)."""
        for kind in kinds:
            assert KIND_WORDS[kind] == words

    def test_image_words_and_photo_words(self):
        """P1 review M7: the usual ru/uk/es/fr image and photo words."""
        assert KIND_WORDS["image"] == ("image", "picture", "imagen", "изображение", "зображення", "картинка",
                                       "illustration")
        assert PHOTO_KIND_WORDS == ("photo", "foto", "фото", "світлина", "photograph", "photographie", "fotografía",
                                    "fotografia", "фотография", "фотографія", "снимок", "знімок", "фотка")

    def test_audio_row_has_no_bare_record_word(self):
        """P1 review m3: the bare "запись"/"запис" stem covered every "записи", so the row says "аудиозапись"."""
        assert not {"запись", "запис"} & set(KIND_WORDS["audio"])

    def test_no_other_kinds(self):
        assert set(KIND_WORDS) == {"image", "pdf", "docx", "doc", "odt", "rtf", "xlsx", "xls", "ods", "csv", "pptx",
                                   "ppt", "odp", "markdown", "text", "html", "mhtml", "eml", "msg", "epub", "video",
                                   "audio", "archive", "folder"}

    def test_every_key_is_a_kind_the_policy_assigns(self):
        assert set(KIND_WORDS) <= KIND_NAMES


class TestEnums:
    def test_job_states(self):
        assert [s.value for s in JobState] == ["current", "ready", "deferred", "parked", "held", "none"]
        assert [h.value for h in HoldReason] == ["refused", "failed"]
        assert [s.value for s in ProfileSource] == ["model", "template"]
        assert [j.value for j in EnrichJob] == ["profile", "passages"]

    def test_hint_and_usage(self):
        assert [t.value for t in HintType] == ["keyword", "question"]
        assert [c.value for c in UsageClass] == ["reader", "triage", "profile", "passage",
                                                  "transcribe"]   # §27.10 (D46), appended

    def test_reply_status(self):
        assert [s.value for s in ModelReplyStatus] == ["ok", "refused", "failed", "invalid", "stopped_at_cap",
                                                       "unavailable"]

    def test_tiers_best_first(self):
        assert [t.value for t in MatchTier] == ["about", "mentioned", "translated", "labelled", "related"]

    def test_source_classes(self):
        assert [c.value for c in SourceClass] == ["verified_record", "passage_words", "translation",
                                                   "automatic_record"]

    def test_key_families(self):
        # xc F1 adds `ef:` (an unprinted entity spelling), xc F3 adds `xl:`/`exl:` (Latin skeletons); §26.3 (Δ58)
        # adds the capture names `cp:` (detected) / `cpa:` (a model's); D40 the scene labels `sl:`; Δ60 values and
        # dates the file's text does not hold `va:` / `ya:` / `ma:`
        assert [f.value for f in KeyFamily] == ["w:", "x:", "xl:", "n:", "m:", "y:", "e:", "ef:", "ex:", "exl:", "pe:",
                                                "v:", "k:", "lw:", "tw:", "dw:", "lv:", "pw:", "pn:", "l:", "cp:",
                                                "cpa:", "sl:", "va:", "ya:", "ma:"]

    def test_family_source_classes(self):
        """A key's class follows from its prefix alone: photo objects are `pe:` and photo numbers `pn:`, automatic
        (review M3); an entity skeleton `ex:` is a translation, never exact (P1 review M8)."""
        # §26.3 (Δ58): a capture code detected is verified (`cp:`), one a model said is automatic (`cpa:`)
        verified = {KeyFamily.ENTITY, KeyFamily.VALUE, KeyFamily.YEAR, KeyFamily.MONTH, KeyFamily.KIND,
                    KeyFamily.CAPTURE}
        # D40: a photograph's scene labels are a model's (`sl:`); Δ60: a value or date the text does not hold is
        # searchable, never exact (`va:`, `ya:`, `ma:`)
        automatic = {KeyFamily.LABEL_WORD, KeyFamily.TOPIC_WORD, KeyFamily.DOMAIN_WORD, KeyFamily.VALIDITY_WORD,
                     KeyFamily.PHOTO_WORD, KeyFamily.PHOTO_ENTITY, KeyFamily.PHOTO_NUMBER, KeyFamily.CAPTURE_AUTO,
                     KeyFamily.SCENE_LABEL, KeyFamily.VALUE_AUTO, KeyFamily.YEAR_AUTO, KeyFamily.MONTH_AUTO}
        # xc F3: Latin skeletons `exl:` too; xc F1: an unprinted entity spelling `ef:` is index-made, never exact
        translation = {KeyFamily.ENTITY_TRANSLIT, KeyFamily.ENTITY_TRANSLIT_LATIN, KeyFamily.ENTITY_FORM}
        assert {f for f, c in FAMILY_SOURCE_CLASS.items() if c is SourceClass.VERIFIED_RECORD} == verified
        assert {f for f, c in FAMILY_SOURCE_CLASS.items() if c is SourceClass.AUTOMATIC_RECORD} == automatic
        assert {f for f, c in FAMILY_SOURCE_CLASS.items() if c is SourceClass.TRANSLATION} == translation
        assert not {f for f, c in FAMILY_SOURCE_CLASS.items() if c is SourceClass.PASSAGE_WORDS}
        # `x:` lives in chunks and names only (never an about key); `w:`/`n:` are not record families; `l:` is never
        # queried.
        # (`xl:` is the Latin twin of `x:`, chunks and names only: xc F3)
        assert set(FAMILY_SOURCE_CLASS) == set(KeyFamily) - {KeyFamily.WORD, KeyFamily.TRANSLIT,
                                                             KeyFamily.TRANSLIT_LATIN, KeyFamily.NUMBER,
                                                             KeyFamily.LABEL}

    def test_skeletons_are_translations_wherever_they_live(self):
        """P1 review M8: a transliteration-only match is "By translation", never exact, in chunks and names too."""
        from src.domain.drive_keys import source_class_of_name_key, source_class_of_word_key
        assert source_class_of_word_key("x:koimbr") is SourceClass.TRANSLATION
        assert source_class_of_name_key("x:koimbr") is SourceClass.TRANSLATION
        assert source_class_of_word_key("w:coimbra") is SourceClass.PASSAGE_WORDS
        assert source_class_of_name_key("w:coimbra") is SourceClass.VERIFIED_RECORD

    def test_no_prefix_is_a_prefix_of_another(self):
        values = [f.value for f in KeyFamily]
        assert not [(a, b) for a in values for b in values if a != b and b.startswith(a)]

    def test_job_usage_classes_and_caps(self):
        assert JOB_USAGE_CLASS == {"profile": "profile", "passages": "passage"}
        assert USD_CAPS == {"reader": P.READER_USD_PER_MONTH, "triage": P.TRIAGE_USD_PER_MONTH,
                            "profile": P.PROFILE_USD_PER_MONTH, "passage": P.PASSAGE_USD_PER_MONTH,
                            "transcribe": P.TRANSCRIBE_USD_PER_MONTH}   # §27.10 (D46): the fifth class

    def test_states_compare_as_plain_strings(self):
        """Record fields hold states as `str` (§25.13.2): the enums are str-valued."""
        assert JobState.READY == "ready" and UsageClass.PROFILE == "profile"


class TestTypes:
    """Field names and order as §25.13.2 freezes them; every type is a frozen dataclass."""

    @pytest.mark.parametrize("cls, expected", [
        (DriveProfile, ["title", "what_it_is", "language", "doc_type", "life_domains", "dates", "entities",
                        "key_values", "topics", "topics_native", "topic_words", "label_words", "domain_words",
                        "validity_words", "graded"]),   # §26.4 (Δ59): every value's evidence grade, appended
        (ProfileDate, ["label", "iso", "printed"]),   # §26.4: the date as printed
        (ProfileEntity, ["name", "type", "printed", "forms", "found"]),   # xc F1: printed spellings tracked
        (KeyValue, ["label", "printed", "canonical"]),
        (PhotoDescription, ["description", "objects", "scene", "visible_text", "scene_labels"]),   # D40, appended
        (PhotoObject, ["name", "forms"]),
        (HintItem, ["text", "type", "evidence", "evidence_start", "evidence_end"]),
        (DriveHintDoc, ["user_id", "item_id", "generation", "index", "kind", "chunk_text_hash", "items", "hint_keys",
                        "passages_version",
                        "connection_id"]),   # MULTI_DRIVE_RFC §8 (E8), appended (positional construction unchanged)
        (EnrichContext, ["now", "month_usd", "caps_usd", "initial_balance_usd", "initial_open"]),
        # P5 review M1: per-class initial-build share, appended (positional construction unchanged)
        (ModelUsageMonth, ["reader_usd", "triage_usd", "profile_usd", "passage_usd", "fallback_usd", "calls",
                           "daily", "reader_initial_usd", "triage_initial_usd", "profile_initial_usd",
                           "passage_initial_usd",
                           # §27.10 (Δ106, G8, G9): the transcribe class, its (always-zero) build share and its open
                           # reservations, appended (positional construction unchanged)
                           "transcribe_usd", "transcribe_initial_usd", "transcribe_reserved_usd"]),
        (InitialBuild, ["allowance_usd", "spent_usd", "opened_at", "closed_at"]),
        (DriveRecordLean, ["item_id", "parent_id", "name", "kind", "effective_date", "about_keys", "name_tokens", "state",
                           "scope", "has_card", "indexed_generation", "valid_until",
                           "owner_note_id",     # DRIVE_FILE_NOTES_RFC §3.3 (review I7)
                           "content_identity"]),   # MULTI_DRIVE_RFC §7.2: replaces content_hash
        (DriveChunkLean, ["user_id", "item_id", "generation", "index", "kind"]),
        (DriveHintLean, ["user_id", "item_id", "generation", "index", "kind", "hint_keys"]),
        (QueryConcepts, ["concepts", "ignored", "cut"]),
        (Concept, ["word", "about_keys", "word_keys", "hint_keys", "is_number"]),
        (ProfileRequest, ["user_id", "account_id", "item_id", "kind", "name", "input", "previous"]),
        (ProfileReply, ["status", "profile", "model", "usd", "fallback_usd"]),
        (HintRequest, ["user_id", "account_id", "item_id", "chunks", "title", "what_it_is"]),
        (HintReply, ["status", "items_by_index", "model", "usd", "fallback_usd", "outcome_by_index"]),
        (ProfileInput, ["head", "tail", "headings", "child_names"]),
        (ProfileLabels, ["doc_type", "life_domains", "valid_until"]),
        (TriageResultV2, ["item_id", "verdict", "photo", "capture"]),   # §26.3: a text image's capture, appended
        (TriageReply, ["status", "results", "model", "usd", "fallback_usd", "model_calls", "capped_item_ids",
                       "refused_item_ids"]),
    ])
    def test_fields(self, cls, expected):
        assert names(cls) == expected
        assert cls.__dataclass_params__.frozen

    def test_profile_constructs(self):
        profile = DriveProfile(
            title="Colosseum entry", what_it_is="Two entry tickets", language="en", doc_type="ticket",
            life_domains=("travel", "leisure"), dates=(ProfileDate("event", date(2026, 5, 12)),),
            entities=(ProfileEntity("Rome", "place", "Rome", ("Roma", "Рим")),),
            key_values=(KeyValue("ticket_number", "No. 0042", "0042"),), topics=("sightseeing",),
            topics_native=("sightseeing",), topic_words=("sightseeing", "turismo"), label_words=("ticket", "квиток"),
            domain_words=("trip", "viaje"), validity_words=())
        assert profile.entities[0].forms == ("Roma", "Рим")
        with pytest.raises(FrozenInstanceError):
            profile.title = "x"  # type: ignore[misc]

    def test_hint_doc_and_lean_types_construct(self):
        assert HintItem("Rome", "keyword", "en Roma").evidence_start == UNGROUNDED == -1
        item = HintItem("Rome", HintType.KEYWORD.value, "reserva en Roma", 13, 28)
        doc = DriveHintDoc("u", "i", 2, 0, "pdf", "h", (item,), ("w:rome",), PASSAGES_VERSION)
        assert doc.items[0].evidence_end - doc.items[0].evidence_start == len(item.evidence)
        lean = DriveRecordLean("i", "p", "entry.pdf", "pdf", NOW, ("e:rome",), ("w:rome",), RecordState.INDEXED, Scope.NORMAL,
                               True, 2, None, content_identity=ContentIdentity("quickxor", "q", 1))
        assert lean.state is RecordState.INDEXED
        assert DriveHintLean("u", "i", 2, 0, "pdf", ("w:rome",)).hint_keys == ("w:rome",)
        assert DriveChunkLean("u", "i", 2, 0, "pdf").index == 0

    def test_usage_types_default_to_zero(self):
        assert ModelUsageMonth() == ModelUsageMonth(0.0, 0.0, 0.0, 0.0, 0.0, 0, {})
        assert InitialBuild(allowance_usd=P.INITIAL_BUILD_USD).closed_at is None
        ctx = EnrichContext(NOW, {UsageClass.PROFILE.value: 1.0}, {UsageClass.PROFILE.value: 4.0}, 40.0, True)
        assert ctx.initial_open

    def test_model_messages_construct(self):
        req = ProfileRequest("u", "a", "i", "pdf", "entry.pdf", ProfileInput(head="text"),
                             ProfileLabels("ticket", ("travel",), date(2026, 5, 12)))
        assert req.input.tail == "" and req.previous is not None
        reply = ProfileReply(ModelReplyStatus.OK, None, "gpt-6-luna", 0.0008)
        assert reply.status is ModelReplyStatus.OK and reply.fallback_usd == 0.0
        hints = HintReply(ModelReplyStatus.OK, {0: (HintItem("Rome", "keyword", "en Roma"),)}, "gpt-6-luna", 0.0004,
                          0.0, {0: ModelReplyStatus.OK, 1: ModelReplyStatus.REFUSED})
        assert hints.outcome_by_index[1] is ModelReplyStatus.REFUSED and 1 not in hints.items_by_index
        assert HintRequest("u", "a", "i", [(0, "text"), (1, "more")], "t", "w").chunks[1] == (1, "more")
        triage = TriageReply(ModelReplyStatus.OK,
                             (TriageResultV2("i", TriageVerdict.PHOTO, PhotoDescription("a fountain")),),
                             "gpt-6-luna", 0.001, capped_item_ids=("j",))
        assert triage.results[0].photo is not None and triage.model_calls == 0 and triage.refused_item_ids == ()

    def test_query_concepts_construct(self):
        concept = Concept("rome", ("e:rome",), ("w:rome",), ("w:rome",), False)
        assert QueryConcepts((concept,), ("all",), ()).concepts[0].word == "rome"


_DT = typing.Optional[datetime]
_STRS = typing.Tuple[str, ...]


class TestFieldTypes:
    """Types as §25.13.2 / §25.2.1 name them (review MINOR 15): a field cannot change type silently."""

    @pytest.mark.parametrize("cls, expected", [
        (DriveRecordLean, {"effective_date": _DT, "valid_until": _DT, "about_keys": _STRS, "name": str,
                           "state": RecordState, "scope": Scope, "indexed_generation": typing.Optional[int]}),
        (ProfileDate, {"label": str, "iso": date}),
        (ProfileLabels, {"valid_until": typing.Optional[date]}),
        (HintItem, {"evidence_start": int, "evidence_end": int}),
        (ModelUsageMonth, {"daily": typing.Mapping[str, typing.Tuple[float, float]], "calls": int,
                           "reader_initial_usd": float, "passage_initial_usd": float}),
        (InitialBuild, {"opened_at": _DT, "closed_at": _DT}),
        (EnrichContext, {"now": datetime, "caps_usd": typing.Mapping[str, float], "initial_open": bool}),
        (ProfileReply, {"status": ModelReplyStatus, "profile": typing.Optional[DriveProfile], "fallback_usd": float}),
        (HintReply, {"outcome_by_index": typing.Mapping[int, ModelReplyStatus]}),
        (DriveSyncState, {"model_backoff_by_agent": typing.Mapping[str, datetime]}),
        (DriveChunk, {"word_keys": _STRS, "keys_version": int}),
        (DriveIndexRecord, {
            "profile": typing.Optional[DriveProfile], "valid_until": _DT, "profile_source": typing.Optional[str],
            "profile_input_fingerprint": typing.Optional[str], "profile_fingerprint": typing.Optional[str],
            "profile_version": int, "profiled_content_version": str, "profile_state": str, "profile_defer_until": _DT,
            "profile_parked_month": str, "profile_hold": typing.Optional[str], "profile_hold_version": str,
            "profile_attempts": int, "passages_state": str, "passages_defer_until": _DT, "passages_parked_month": str,
            "passages_hold": typing.Optional[str], "passages_hold_version": str, "passages_attempts": int,
            "passages_generation": typing.Optional[int], "passages_next_index": int, "passages_done_at": _DT,
            "enrich_ready": bool, "enrich_wake_at": _DT, "effective_day": str, "keys_pending": bool,
            "keys_version": int, "keys_capped": bool, "about_keys": _STRS, "place_forms": _STRS,
            "photo_description": str, "photo_objects": typing.Tuple[PhotoObject, ...], "photo_scene": str,
            "visible_text": str, "readme_excerpt": str, "readme_excerpt_version": str}),
    ])
    def test_types(self, cls, expected):
        hints = typing.get_type_hints(cls)
        assert {name: hints[name] for name in expected} == expected

    def test_record_v2_defaults_are_neutral(self):
        rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="i")
        assert (rec.profile_state, rec.passages_state) == (JobState.NONE.value, JobState.NONE.value)
        assert rec.caption == "" and rec.lead_text == ""   # kept until P3/P5 retire or reuse them


class TestChunkFields:
    def test_word_keys_and_keys_version_default_empty(self):
        chunk = DriveChunk("u", "i", 1, 0, "text", 0, 4, "pdf")
        assert chunk.word_keys == () and chunk.keys_version == 0 and chunk.content_terms == ()


class TestParameters:
    def test_table(self):
        from datetime import timedelta
        assert (P.PROFILE_MIN_CHARS, P.PROFILE_REFRESH_COOLDOWN, P.PASSAGE_REFRESH_COOLDOWN) == (
            200, timedelta(hours=24), timedelta(hours=24))
        assert (P.PASSAGE_CHUNKS_PER_CALL, P.PASSAGE_MAX_CHUNKS_PER_FILE, P.PASSAGE_REUSE_SHARE,
                P.PASSAGE_MIN_SIMILARITY) == (2, 100, 0.8, 0.5)
        assert P.TRIAGE_BATCH_V2 == 8
        assert (P.PROFILER_OUTPUT_TOKENS, P.PASSAGES_OUTPUT_TOKENS, P.TRIAGE_OUTPUT_TOKENS) == (4096, 4096, 8192)
        assert (P.READER_USD_PER_MONTH, P.TRIAGE_USD_PER_MONTH, P.PROFILE_USD_PER_MONTH,
                P.PASSAGE_USD_PER_MONTH) == (4, 3, 4, 20)
        assert (P.INITIAL_BUILD_USD, P.FALLBACK_ALERT_SHARE) == (40, 0.5)
        assert (P.ENRICH_SHARE, P.ENRICH_IN_FLIGHT, P.PASSAGE_IN_FLIGHT, P.ENRICH_FILE_BUDGET_S) == (0.4, 3, 2, 300)
        assert (P.WORD_KEYS_PER_CHUNK, P.HINT_KEYS_PER_CHUNK, P.ABOUT_KEYS_PER_RECORD) == (1500, 60, 600)  # P1 B2
        assert (P.WRITE_BATCH_BYTES, P.README_SCAN_CHILDREN, P.MAX_CONCEPTS) == (6_000_000, 1000, 8)
        assert (P.ABOUT_FETCH, P.WORD_FETCH, P.HINT_FETCH, P.WORD_FILES_MAX) == (500, 300, 300, 60)
        assert (P.CARD_FETCH, P.CHUNK_FETCH) == (50, 300)
        assert (P.MEANING_POOL, P.MEANING_POOL_MIN, P.MIN_SPREAD) == (50, 12, 0.01)
        assert (P.Z_RELATED, P.DRIVE_SIZE_BANDS) == (2.5, (200, 2000, 10000))
        assert (P.RELATED_MAX, P.RELATED_SMALL, P.COMMON_SHARE, P.PROXIMITY_CHARS) == (3, 2, 0.3, 400)
        assert (P.ABOUT_SHOWN, P.MENTIONED_SHOWN, P.TRANSLATED_SHOWN, P.LABELLED_SHOWN) == (10, 6, 6, 8)
        assert (P.CARD_RENDER_MAX, P.RESULT_CHARS_MAX) == (700, 16_000)
        assert (P.WINDOW_PAD, P.WINDOW_MAX, P.WINDOW_CHARS_TOTAL_V2) == (300, 900, 5000)
        assert (P.GROUP_LINES, P.GROUP_READ_MAX, P.CONCEPT_KEYS_MAX) == (6, 1000, 30)

    def test_shared_caps(self):
        """Caps more than one part reads (review M6): §25.2.6, §25.3, §25.4.1-§25.4.4, §25.5.1."""
        assert (P.PROFILE_ENTITIES_MAX, P.ENTITY_FORMS_MAX, P.PROFILE_KEY_VALUES_MAX, P.PROFILE_DATES_MAX,
                P.PROFILE_TOPICS_MAX, P.PROFILE_WORD_LIST_MAX) == (12, 8, 12, 5, 8, 24)
        assert (P.HINT_KEYWORDS_MAX, P.HINT_QUESTIONS_MAX, P.HINT_EVIDENCE_WORDS_MAX) == (12, 3, 12)
        assert (P.PHOTO_DESCRIPTION_CHARS, P.PHOTO_OBJECTS_MAX, P.PHOTO_OBJECT_FORMS_MAX, P.PHOTO_SCENE_WORDS,
                P.PHOTO_VISIBLE_TEXT_CHARS) == (240, 8, 6, 6, 200)
        assert (P.PROFILE_HEAD_CHARS, P.PROFILE_TAIL_CHARS, P.PROFILE_HEADINGS_CHARS, P.PROFILE_HEADINGS_MIN_TEXT,
                P.PROFILE_EPUB_BODY_CHARS, P.PROFILE_SHEET_ROWS) == (6000, 1500, 1500, 20_000, 3000, 20)
        assert (P.README_EXCERPT_HEAD_CHARS, P.README_EXCERPT_TAIL_CHARS, P.README_CHILD_NAMES) == (6000, 1500, 200)
        assert P.PHRASE_RUN_MAX == 3
        assert (P.PROFILER_TIMEOUT_S, P.PASSAGES_TIMEOUT_S, P.TRIAGE_TIMEOUT_S) == (60, 60, 120)

    def test_byte_batches_fit_a_firestore_request(self):
        assert P.WRITE_BATCH_BYTES < 10 * 1024 * 1024

    def test_the_clashing_v1_values_are_replaced(self):
        """The two names that clashed with v1: P3 replaced TRIAGE_BATCH with the v2 value (§25.4.3, §25.12: 8 photos
        per enriched call); P6 replaced the v1 12 000-char window budget with WINDOW_CHARS_TOTAL_V2 (§25.5.6), and the
        v1 constant is removed (reviewer, 2026-10-13: the cleanup spec change)."""
        assert (P.TRIAGE_BATCH, P.TRIAGE_BATCH_V2, P.WINDOW_CHARS_TOTAL_V2) == (8, 8, 5_000)
        assert not hasattr(P, "WINDOW_CHARS_TOTAL")


class TestTexts:
    def test_render_example_reassembles(self):
        """The §25.13.6 synthetic result's lines, rebuilt from the templates."""
        assert T.SEARCHED_LINE.format(concepts='"rome", "tickets"') + T.SEARCHED_IGNORED.format(
            words='"all", "my"') + "." == (
            'Searched names (several spellings and word forms), file descriptions, document text and passage '
            'keywords for: "rome", "tickets"; ignored: "all", "my".')
        parts = "; ".join(["4 text files are " + T.COVERAGE_NOT_DESCRIBED, "1 document could not be read"])
        assert T.COVERAGE_INCOMPLETE.format(parts=parts) == (
            "Coverage: incomplete — 4 text files are not described yet; 1 document could not be read.")
        assert T.COVERAGE_COMPLETE == "Coverage: complete."
        assert T.ABOUT_HEADING.format(query="rome tickets", count=1) == 'About "rome tickets" (1):'
        assert T.MENTIONED_HEADING.format(count=1) == "Mentioned in (1):"
        assert T.TRANSLATED_HEADING.format(count=1) == "By translation (automatic) (1):"
        assert T.LABELLED_HEADING.format(count=T.SHOWING.format(shown=2, total=5)) == (
            "Labelled (automatic) (showing 2 of 5):")
        assert T.RELATED_HEADING.format(count=1) == "Related by meaning only, unverified (1):"
        assert T.MATCHED_LINE.format(sources="; ".join([T.MATCH_PLACE_IN_FILE.format(word="Rome"),
                                                         T.MATCH_NAME.format(word="tickets")])) == (
            'matched: place "Rome" (named in the file); name "tickets"')
        assert T.MATCH_WORDS_IN_PASSAGE.format(words='"Rome", "tickets"') == 'words "Rome", "tickets" in one passage'
        assert T.MATCH_PASSAGE_KEYWORD.format(word="Rome", evidence="reserva en Roma, 3 noches") == (
            'passage keyword "Rome", from "reserva en Roma, 3 noches"')
        assert T.MATCH_TYPE_LABEL.format(word="ticket") == 'type label "ticket" (automatic)'
        assert T.DATE_LINE.format(iso="2026-05-12", source="written in the document") == (
            "date: 2026-05-12 (written in the document)")
        assert T.GROUP_LINE.format(folder="Travel/Rome", count=3, tier="labelled", kinds="PDF", dates="2026-05") == (
            "Travel/Rome: 3 more labelled files (PDF, 2026-05)")
        assert T.FOOTER_EXACT == "No other file is about or mentions all of these words."

    def test_d29_and_footers(self):
        assert T.ABSENT_LINE.format(word="vaccination") + " " + T.WITHOUT_LINE.format(word="vaccination", count=3) == (
            'No file holds "vaccination". Without "vaccination": 3 files match the other words.')
        terms = ", ".join([T.CONJUNCTION_TERM.format(word="coimbra", count=41),
                           T.CONJUNCTION_TERM.format(word="trip", count=2)])
        assert T.FOOTER_NO_CONJUNCTION.format(terms=terms) == (
            'No file matches all of: "coimbra" (41 files), "trip" (2 files).')
        assert T.MIGRATION_LINE == ("The index is being migrated to a new search format: only names, meaning and "
                                    "filters were searched.")

    def test_no_footer_says_final(self):
        footers = (T.FOOTER_EXACT, T.FOOTER_NO_CONJUNCTION, T.ABSENT_LINE, T.WITHOUT_LINE, T.WITHOUT_AT_LEAST_LINE)
        assert not any("final" in text.lower() for text in footers)

    def test_protocol_tiers_line_equals_the_rfc(self):
        assert T.PROTOCOL_TIERS_LINE == rfc_quote('Protocol `tiers` line: "', '"\n')

    def test_d29_footer_and_migration_lines_equal_the_rfc(self):
        assert T.ABSENT_LINE.format(word="vaccination") + " " + T.WITHOUT_LINE.format(word="vaccination", count=3) == (
            rfc_quote("A blocked concept: `", "`"))
        assert T.MIGRATION_LINE == rfc_quote("Migration: `", "`")
