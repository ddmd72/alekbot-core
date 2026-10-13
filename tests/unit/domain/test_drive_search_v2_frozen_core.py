"""Search v2 frozen core (USER_DRIVE_INDEXING_RFC §25.13): the types, label sets, `k:` table, version constants,
parameters and verbatim texts the plan parts are written against. Pins the surface only — no behaviour."""
from dataclasses import FrozenInstanceError, fields
from datetime import date, datetime, timezone

import pytest

from src.domain import drive_index_params as P
from src.domain import drive_search_texts as T
from src.domain.drive_enrichment import (
    DriveHintDoc, DriveProfile, EnrichContext, EnrichJob, HintItem, HintType, HoldReason, InitialBuild, JobState,
    KeyValue, ModelUsageMonth, PhotoDescription, PhotoObject, ProfileDate, ProfileEntity, ProfileSource, UsageClass,
)
from src.domain.drive_enrichment_calls import (
    HintReply, HintRequest, ModelReplyStatus, ProfileInput, ProfileLabels, ProfileReply, ProfileRequest, TriageReply,
    TriageResultV2,
)
from src.domain.drive_index import (
    CURRENT_VERSIONS, KEYS_VERSION, MIGRATION_GATE_KEY, PASSAGES_VERSION, PROFILE_VERSION, SEARCH_V2_VERSIONS,
    DriveChunk, RecordState, Scope, TriageVerdict,
)
from src.domain.drive_index_lean import DriveChunkLean, DriveHintLean, DriveRecordLean
from src.domain.drive_labels import (
    DATE_LABELS, DOC_TYPES, ENTITY_TYPES, KIND_WORDS, LIFE_DOMAINS, PHOTO_KIND_WORDS, VALUE_LABELS,
)
from src.domain.drive_match import Concept, KeyFamily, MatchTier, QueryConcepts, SourceClass

NOW = datetime(2026, 10, 13, tzinfo=timezone.utc)


def names(cls):
    return [f.name for f in fields(cls)]


class TestVersions:
    def test_v2_keys_are_defined(self):
        assert SEARCH_V2_VERSIONS == {"keys": KEYS_VERSION, "profile": PROFILE_VERSION, "passages": PASSAGES_VERSION}
        assert all(isinstance(v, int) and v >= 1 for v in SEARCH_V2_VERSIONS.values())
        assert MIGRATION_GATE_KEY == "keys"

    def test_v2_keys_are_not_current_yet(self):
        """Adding them to CURRENT_VERSIONS would start the v1 upgrade pass: P5 does it behind the migration gate."""
        assert not set(SEARCH_V2_VERSIONS) & set(CURRENT_VERSIONS)


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
            "reference", "tax_id", "phone", "amount_total", "amount_due", "price", "other_id")

    def test_unstable_labels_stay_out(self):
        assert "personal_record" not in DOC_TYPES and "issuer" not in ENTITY_TYPES + VALUE_LABELS


class TestKindTable:
    @pytest.mark.parametrize("kinds, words", [
        (("pdf",), ("pdf",)),
        (("docx", "doc", "odt", "rtf"), ("document", "documento", "документ")),
        (("xlsx", "xls", "ods", "csv"), ("spreadsheet", "table", "hoja de cálculo", "таблица", "таблиця")),
        (("pptx", "ppt", "odp"), ("presentation", "slides", "presentación", "презентация", "презентація")),
        (("markdown", "text"), ("note", "text", "nota", "заметка", "нотатка")),
        (("html",), ("web page", "página web", "веб-страница", "веб-сторінка")),
        (("eml", "msg"), ("email", "mail", "correo", "письмо", "лист")),
        (("epub",), ("book", "e-book", "libro", "книга")),
        (("video",), ("video", "vídeo", "видео", "відео")),
        (("audio",), ("audio", "recording", "grabación", "запись", "запис")),
        (("archive",), ("archive", "zip", "archivo", "архив", "архів")),
        (("folder",), ("folder", "carpeta", "папка")),
    ])
    def test_rows(self, kinds, words):
        for kind in kinds:
            assert KIND_WORDS[kind] == words

    def test_image_words_and_photo_words(self):
        assert set(KIND_WORDS["image"]) == {"image", "picture", "imagen"}
        assert PHOTO_KIND_WORDS == ("photo", "foto", "фото", "світлина")

    def test_no_other_kinds(self):
        assert set(KIND_WORDS) == {"image", "pdf", "docx", "doc", "odt", "rtf", "xlsx", "xls", "ods", "csv", "pptx",
                                   "ppt", "odp", "markdown", "text", "html", "eml", "msg", "epub", "video", "audio",
                                   "archive", "folder"}


class TestEnums:
    def test_job_states(self):
        assert [s.value for s in JobState] == ["current", "ready", "deferred", "parked", "held", "none"]
        assert [h.value for h in HoldReason] == ["refused", "failed"]
        assert [s.value for s in ProfileSource] == ["model", "template"]
        assert [j.value for j in EnrichJob] == ["profile", "passages"]

    def test_hint_and_usage(self):
        assert [t.value for t in HintType] == ["keyword", "question"]
        assert [c.value for c in UsageClass] == ["reader", "triage", "profile", "passage"]

    def test_reply_status(self):
        assert [s.value for s in ModelReplyStatus] == ["ok", "refused", "failed", "stopped_at_cap", "unavailable"]

    def test_tiers_best_first(self):
        assert [t.value for t in MatchTier] == ["about", "mentioned", "translated", "labelled", "related"]

    def test_source_classes(self):
        assert [c.value for c in SourceClass] == ["verified_record", "passage_words", "translation",
                                                   "automatic_record"]

    def test_key_families(self):
        assert [f.value for f in KeyFamily] == ["w:", "n:", "m:", "y:", "e:", "v:", "k:", "lw:", "tw:", "dw:", "lv:",
                                                "pw:", "l:"]

    def test_states_compare_as_plain_strings(self):
        """Record fields hold states as `str` (§25.13.2): the enums are str-valued."""
        assert JobState.READY == "ready" and UsageClass.PROFILE == "profile"


class TestTypes:
    """Field names and order as §25.13.2 freezes them; every type is a frozen dataclass."""

    @pytest.mark.parametrize("cls, expected", [
        (DriveProfile, ["title", "what_it_is", "language", "doc_type", "life_domains", "dates", "entities",
                        "key_values", "topics", "topics_native", "topic_words", "label_words", "domain_words",
                        "validity_words"]),
        (ProfileDate, ["label", "iso"]),
        (ProfileEntity, ["name", "type", "printed", "forms"]),
        (KeyValue, ["label", "printed", "canonical"]),
        (PhotoDescription, ["description", "objects", "scene", "visible_text"]),
        (PhotoObject, ["name", "forms"]),
        (HintItem, ["text", "type", "evidence", "evidence_start", "evidence_end"]),
        (DriveHintDoc, ["user_id", "item_id", "generation", "index", "kind", "chunk_text_hash", "items", "hint_keys",
                        "passages_version"]),
        (EnrichContext, ["now", "month_usd", "caps_usd", "initial_balance_usd", "initial_open"]),
        (ModelUsageMonth, ["reader_usd", "triage_usd", "profile_usd", "passage_usd", "fallback_usd", "calls",
                           "daily"]),
        (InitialBuild, ["allowance_usd", "spent_usd", "opened_at", "closed_at"]),
        (DriveRecordLean, ["item_id", "parent_id", "kind", "effective_date", "about_keys", "name_tokens", "state",
                           "scope", "has_card", "indexed_generation", "content_hash", "valid_until"]),
        (DriveChunkLean, ["user_id", "item_id", "generation", "index", "kind"]),
        (DriveHintLean, ["user_id", "item_id", "generation", "index", "kind", "hint_keys"]),
        (QueryConcepts, ["concepts", "ignored", "cut"]),
        (Concept, ["word", "about_keys", "word_keys", "hint_keys", "is_number"]),
        (ProfileRequest, ["user_id", "account_id", "item_id", "kind", "name", "input", "previous"]),
        (ProfileReply, ["status", "profile", "model", "usd", "fallback_used"]),
        (HintRequest, ["user_id", "account_id", "item_id", "chunks", "title", "what_it_is"]),
        (HintReply, ["status", "items_by_index", "model", "usd", "fallback_used"]),
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
        item = HintItem("Rome", HintType.KEYWORD.value, "reserva en Roma", 13, 28)
        doc = DriveHintDoc("u", "i", 2, 0, "pdf", "h", (item,), ("w:rome",), PASSAGES_VERSION)
        assert doc.items[0].evidence_end - doc.items[0].evidence_start == len(item.evidence)
        lean = DriveRecordLean("i", "p", "pdf", NOW, ("e:rome",), ("w:rome",), RecordState.INDEXED, Scope.NORMAL,
                               True, 2, "q:1", None)
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
        reply = ProfileReply(ModelReplyStatus.OK, None, "gpt-6-luna", 0.0008, False)
        assert reply.status is ModelReplyStatus.OK
        hints = HintReply(ModelReplyStatus.REFUSED, {}, "gpt-6-luna", 0.0, False)
        assert HintRequest("u", "a", "i", [(0, "text"), (1, "more")], "t", "w").chunks[1] == (1, "more")
        assert hints.items_by_index == {}
        triage = TriageReply((TriageResultV2("i", TriageVerdict.PHOTO, PhotoDescription("a fountain")),),
                             "gpt-6-luna", 0.001, False)
        assert triage.results[0].photo is not None and triage.model_calls == 0

    def test_query_concepts_construct(self):
        concept = Concept("rome", ("e:rome",), ("w:rome",), ("w:rome",), False)
        assert QueryConcepts((concept,), ("all",), ()).concepts[0].word == "rome"


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
        assert (P.WORD_KEYS_PER_CHUNK, P.HINT_KEYS_PER_CHUNK, P.ABOUT_KEYS_PER_RECORD) == (900, 60, 600)
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

    def test_byte_batches_fit_a_firestore_request(self):
        assert P.WRITE_BATCH_BYTES < 10 * 1024 * 1024

    def test_v1_values_still_in_use_are_unchanged(self):
        """The two clashing names keep their v1 values until P3 / P6 replace them."""
        assert (P.TRIAGE_BATCH, P.WINDOW_CHARS_TOTAL) == (16, 12_000)


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
        terms = ", ".join([T.CONJUNCTION_TERM.format(word="barcelona", count=41),
                           T.CONJUNCTION_TERM.format(word="trip", count=2)])
        assert T.FOOTER_NO_CONJUNCTION.format(terms=terms) == (
            'No file matches all of: "barcelona" (41 files), "trip" (2 files).')
        assert T.MIGRATION_LINE == ("The index is being migrated to a new search format: only names, meaning and "
                                    "filters were searched.")

    def test_no_footer_says_final(self):
        footers = (T.FOOTER_EXACT, T.FOOTER_NO_CONJUNCTION, T.ABSENT_LINE, T.WITHOUT_LINE, T.WITHOUT_AT_LEAST_LINE)
        assert not any("final" in text.lower() for text in footers)

    def test_manifest_texts(self):
        assert T.DRIVE_QUERY_DESCRIPTION.startswith("search_files_in_drive: REQUIRED unless the request is filter-only")
        assert "a time span ('last year', '2023–2024') goes in drive_date_from / drive_date_to, not in the words" in (
            T.DRIVE_QUERY_DESCRIPTION)
        assert T.DRIVE_QUERY_DESCRIPTION.endswith("Leave EMPTY only for filter-only requests.")
        assert "document (docx, doc, odt, rtf, pdf, markdown, text)" in T.DRIVE_KIND_DESCRIPTION
        assert T.DRIVE_DATE_FROM_DESCRIPTION == (
            "search_files_in_drive: optional earliest date, YYYY-MM-DD, inclusive: a photo's taken date, otherwise "
            "the date written in the document when the index found one, otherwise the file's date. Use it for a "
            "time span the user gives.")

    def test_protocol_tiers_line(self):
        assert T.PROTOCOL_TIERS_LINE.startswith("Drive results say how each file matched.")
        assert T.PROTOCOL_TIERS_LINE.endswith("so you can tell whether the word was phrasing or the subject.")
        assert "'Related by meaning only' lines are guesses." in T.PROTOCOL_TIERS_LINE
