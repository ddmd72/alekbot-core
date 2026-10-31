"""drive_profile (USER_DRIVE_INDEXING_RFC §25.3, §25.4.1): the per-kind policy table, the profiler's input from stored
chunk text, the input fingerprint, templates, and verification of the model's profile against the text. Synthetic
texts only."""
from dataclasses import replace
from datetime import date

import pytest

from src.domain import drive_profile as dp
from src.domain.drive_enrichment import DriveProfile, KeyValue, ProfileDate, ProfileEntity
from src.domain.drive_enrichment_calls import ProfileInput
from src.domain.drive_index import DriveChunk, DriveIndexRecord, Reason, TriageVerdict
from src.domain.drive_index_params import (
    PROFILE_ENTITIES_MAX, PROFILE_HEAD_CHARS, PROFILE_HEADINGS_CHARS, PROFILE_TAIL_CHARS, PROFILE_WORD_LIST_MAX,
    README_CHILD_NAMES,
)
from src.domain.drive_labels import DOC_TYPES, LIFE_DOMAINS

I, O = dp.InputRule, dp.ProfileOutput


def _chunks(text, size=2400, overlap=0):
    """Chunks over `text` as the chunker lays them out: spans with an optional overlap."""
    out, start, index = [], 0, 0
    while start < len(text):
        end = min(len(text), start + size)
        s = max(0, start - overlap) if index else 0
        out.append(DriveChunk(user_id="u", connection_id="u", item_id="i", generation=1, index=index, text=text[s:end], char_start=s,
                              char_end=end, kind="pdf"))
        start, index = end, index + 1
    return out


def _rec(kind="pdf", **kw):
    return DriveIndexRecord(user_id="u", connection_id="u", item_id="i", name=kw.pop("name", "file"), kind=kind, **kw)


def _para(n, word="lorem"):
    return "\n\n".join(f"Paragraph {i} {word} ipsum dolor sit amet, consectetur adipiscing elit." for i in range(n))


class TestPolicyTable:
    @pytest.mark.parametrize("kind", ["pdf", "docx", "odt", "rtf", "html", "markdown", "text"])
    def test_documents_get_a_full_profile_and_passages(self, kind):
        assert dp.PROFILE_POLICY[kind] == dp.KindPolicy(I.DOCUMENT, O.FULL, True)

    @pytest.mark.parametrize("kind", ["pptx", "odp"])
    def test_slides(self, kind):
        assert dp.PROFILE_POLICY[kind] == dp.KindPolicy(I.SLIDES, O.FULL, True)

    @pytest.mark.parametrize("kind, rule", [("xlsx", I.SHEETS), ("xls", I.SHEETS), ("ods", I.SHEETS),
                                            ("csv", I.SHEETS), ("json", I.JSON), ("epub", I.EPUB),
                                            ("folder", I.README)])
    def test_no_key_values_and_no_passages(self, kind, rule):
        assert dp.PROFILE_POLICY[kind] == dp.KindPolicy(rule, O.NO_KEY_VALUES, False)

    @pytest.mark.parametrize("kind, rule", [("eml", I.EMAIL), ("msg", I.EMAIL), ("subtitles", I.CUES),
                                            ("audio", I.CUES), ("video", I.CUES)])
    def test_mail_and_cues(self, kind, rule):
        assert dp.PROFILE_POLICY[kind] == dp.KindPolicy(rule, O.FULL, True)

    @pytest.mark.parametrize("kind", ["code", "log", "archive", "disk_image", "database", "executable",
                                      "data_dump", "other"])
    def test_no_profile(self, kind):
        assert dp.policy_of(kind, text_chars=10_000) == dp.NO_POLICY

    def test_a_scan_read_by_the_reader_is_a_document(self):
        assert dp.policy_of("image", text_chars=5_000, triaged_text=True) == dp.PROFILE_POLICY["pdf"]
        assert dp.policy_of("image", text_chars=5_000) == dp.NO_POLICY

    def test_overrides(self):
        assert dp.policy_of("html", text_chars=9_000, script_rendered=True) == dp.KindPolicy(
            I.NONE, O.TEMPLATE, False)
        assert dp.policy_of("pdf", text_chars=9_000, machine_text=True) == dp.KindPolicy(I.NONE, O.TEMPLATE, False)
        assert dp.policy_of("markdown", text_chars=199) == dp.KindPolicy(I.NONE, O.TEMPLATE, False)
        assert dp.policy_of("markdown", text_chars=200) == dp.PROFILE_POLICY["markdown"]
        assert dp.policy_of("folder", text_chars=0) == dp.NO_POLICY
        assert dp.policy_of("folder", text_chars=0, readme=True) == dp.PROFILE_POLICY["folder"]

    def test_record_policy_reads_the_record(self):
        assert dp.record_policy(_rec("pdf", reason=Reason.MACHINE_TEXT), text_chars=5_000).output is O.TEMPLATE
        assert dp.record_policy(_rec("image", triage_verdict=TriageVerdict.TEXT), text_chars=5_000).doc2query
        assert dp.record_policy(_rec("folder", readme_excerpt="# Readme"), text_chars=0).input_rule is I.README

    def test_the_labels_used_by_templates_are_in_the_fixed_set(self):
        assert {"notes", "web_page", "other"} <= set(DOC_TYPES)


class TestStoredText:
    def test_reassembles_chunks_without_their_overlap(self):
        text = _para(200)
        assert dp.stored_text(_chunks(text, overlap=240)) == text

    def test_a_repeated_table_header_outside_the_span_is_dropped(self):
        source = "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |"
        first = DriveChunk("u", "i", 1, 0, source[:28], 0, 28, "csv")
        header = "| a | b |\n|---|---|\n"
        second = DriveChunk("u", "i", 1, 1, header + source[28:], 28, len(source), "csv")
        assert dp.stored_text([second, first]) == source

    def test_a_gap_becomes_a_paragraph_break(self):
        a = DriveChunk("u", "i", 1, 0, "first", 0, 5, "pdf")
        b = DriveChunk("u", "i", 1, 1, "second", 9, 15, "pdf")
        assert dp.stored_text([a, b]) == "first\n\nsecond"


class TestProfileInput:
    def test_a_short_document_is_all_head(self):
        text = _para(20)
        inp = dp.profile_input(_rec(), _chunks(text), "pdf")
        assert inp == ProfileInput(head=text)

    def test_head_and_tail_cut_at_paragraph_boundaries(self):
        text = _para(400)
        inp = dp.profile_input(_rec(), _chunks(text), "pdf")
        assert 0.5 * PROFILE_HEAD_CHARS < len(inp.head) <= PROFILE_HEAD_CHARS
        assert len(inp.tail) <= PROFILE_TAIL_CHARS and inp.tail.startswith("Paragraph")
        assert inp.head.endswith("elit.") and text.startswith(inp.head) and text.endswith(inp.tail)
        assert inp.headings == ()   # a text under PROFILE_HEADINGS_MIN_TEXT gets none

    def test_headings_over_twenty_thousand_chars(self):
        body = "\n\n".join(f"## Section {i}\n\n" + _para(6) for i in range(80))
        inp = dp.profile_input(_rec("docx"), _chunks(body), "docx")
        assert inp.headings[:2] == ("## Section 0", "## Section 1")
        assert sum(len(h) + 1 for h in inp.headings) <= PROFILE_HEADINGS_CHARS

    def test_slides_always_carry_their_titles(self):
        text = ("<!-- Slide number: 1 -->\n# Quarterly plan\n\n" + _para(3) + "\n\n<!-- Slide number: 2 -->\n"
                "# Budget\n\n" + _para(3))
        inp = dp.profile_input(_rec("pptx"), _chunks(text), "pptx")
        assert inp.headings == ("# Quarterly plan", "# Budget")

    def test_sheets_keep_names_headers_and_twenty_rows(self):
        rows = "\n".join(f"| {i} | item {i} |" for i in range(60))
        text = f"## Expenses\n\n| n | what |\n|---|---|\n{rows}\n\n## Income\n\n| n | from |\n|---|---|\n| 1 | job |"
        inp = dp.profile_input(_rec("xlsx"), _chunks(text), "xlsx")
        assert inp.headings == ("Expenses", "Income")
        assert "| 19 | item 19 |" in inp.head and "| 20 | item 20 |" not in inp.head
        assert "| 1 | job |" in inp.head and inp.tail == ""

    def test_csv_is_one_table(self):
        rows = "\n".join(f"| {i} | x |" for i in range(40))
        inp = dp.profile_input(_rec("csv"), _chunks(f"| n | v |\n|---|---|\n{rows}"), "csv")
        assert inp.head.startswith("| n | v |") and "| 19 | x |" in inp.head and "| 20 | x |" not in inp.head

    def test_epub_metadata_and_the_start_of_the_body(self):
        text = "**Title:** A Tale\n**Authors:** Someone\n\n" + _para(300)
        inp = dp.profile_input(_rec("epub"), _chunks(text), "epub")
        assert inp.head.startswith("**Title:** A Tale\n**Authors:** Someone")
        assert len(inp.head) < 3_200 and inp.tail == ""

    def test_mail_headers_then_the_body(self):
        text = "Subject: Hello\nFrom: a@example.org\n\n" + _para(400)
        inp = dp.profile_input(_rec("eml"), _chunks(text), "eml")
        assert inp.head.startswith("Subject: Hello\nFrom: a@example.org\n\n")
        assert inp.tail and len(inp.tail) <= PROFILE_TAIL_CHARS

    def test_json_head_only(self):
        text = '{"a": ' + ", ".join(f'"k{i}": {i}' for i in range(2000)) + "}"
        inp = dp.profile_input(_rec("json"), _chunks(text), "json")
        assert len(inp.head) <= PROFILE_HEAD_CHARS and inp.tail == ""

    def test_readme_of_a_collapsed_folder(self):
        excerpt = dp.readme_excerpt("# Project\n\nA tool that does things.", ["src", "README.md", "setup.py"])
        inp = dp.profile_input(_rec("folder", readme_excerpt=excerpt), [], "folder")
        assert inp.head.startswith("# Project") and inp.child_names == ("src", "README.md", "setup.py")

    def test_readme_excerpt_bounds(self):
        excerpt = dp.readme_excerpt(_para(400), [f"f{i}" for i in range(500)])
        inp = dp.profile_input(_rec("folder", readme_excerpt=excerpt), [], "folder")
        assert len(inp.child_names) == README_CHILD_NAMES
        assert len(inp.head) <= PROFILE_HEAD_CHARS and len(inp.tail) <= PROFILE_TAIL_CHARS

    @pytest.mark.parametrize("rec, chunks", [
        (_rec("pdf", reason=Reason.MACHINE_TEXT), _chunks(_para(50))),
        (_rec("markdown"), _chunks("short note")),
        (_rec("code"), _chunks(_para(50))),
        (_rec("folder"), []),
        (_rec("pdf"), []),
    ])
    def test_no_model_input(self, rec, chunks):
        assert dp.profile_input(rec, chunks, rec.kind) is None


class TestFingerprint:
    def test_changes_with_the_input_and_kind_not_with_the_name(self):
        inp = ProfileInput(head="abc", tail="xyz")
        fp = dp.input_fingerprint("pdf", inp)
        assert fp == dp.input_fingerprint("pdf", ProfileInput(head="abc", tail="xyz"))
        assert fp != dp.input_fingerprint("docx", inp)
        assert fp != dp.input_fingerprint("pdf", replace(inp, tail="xyz!"))
        assert fp != dp.input_fingerprint("pdf", replace(inp, child_names=("a",)))
        assert len(fp) == 32


class TestTemplates:
    def test_a_short_note(self):
        p = dp.template_profile(dp.TemplateReason.SHORT, "markdown", "Buy milk\n and  bread")
        assert (p.doc_type, p.what_it_is, p.title) == ("notes", "Buy milk and bread", "")

    def test_a_script_page(self):
        assert dp.template_profile(dp.TemplateReason.SCRIPT_RENDERED, "html", "x").doc_type == "web_page"

    def test_machine_text(self):
        p = dp.template_profile(dp.TemplateReason.MACHINE_TEXT, "text", "a b c")
        assert (p.doc_type, p.what_it_is) == ("other", "machine-generated text")


TEXT = ("Factura nº FA-2024/0137. Fecha: 15/03/2024. Cliente: João Exemplo, Springfield. Total: 1.250,00 €. "
        "Válido hasta el 30 de abril de 2024. Cuenta XX12 3456 7890.")


def _profile(**kw):
    base = dict(title="Invoice", what_it_is="An invoice", language="es", doc_type="invoice",
                life_domains=("finance",), topic_words=("invoice", "factura"))
    base.update(kw)
    return DriveProfile(**base)


def _grades(p, field):
    return [(g.value, g.grade) for g in p.graded if g.field == field]


class TestVerify:
    """Δ59 (§26.4): `grade` replaced `verify`. A value the text does not hold is kept, graded `not_in_text`, and
    counted in `GradeCounts.not_in_text` (it was a drop counted in `VerifyDrops`)."""

    def _verify(self, profile, text=TEXT, name="factura.pdf", **kw):
        return dp.grade(profile, _chunks(text), name, **kw)

    def test_entities_printed_in_the_text_or_the_name_stay(self):
        p, counts = self._verify(_profile(entities=(
            ProfileEntity("Springfield", "place", "Springfield", ("Спрінгфілд",)),
            ProfileEntity("Joao Exemplo", "person", "JOÃO EXEMPLO"),
            ProfileEntity("Factura", "other", "factura.pdf"),
            ProfileEntity("Hamburg", "place", "Hamburg"))))
        assert [e.name for e in p.entities] == ["Springfield", "Joao Exemplo", "Factura", "Hamburg"]   # kept (Δ59)
        assert _grades(p, "entity") == [("Springfield", "in_text"), ("JOÃO EXEMPLO", "in_text"),
                                        ("factura.pdf", "in_text"), ("Hamburg", "not_in_text")]
        assert p.entities[0].forms == ("Спрінгфілд",) and counts.not_in_text == {"entity": 1}

    def test_key_values_by_number_terms_or_verbatim_and_canonical_rewritten(self):
        p, counts = self._verify(_profile(key_values=(
            KeyValue("amount_total", "1.250,00 €", "1250.00 EUR"),
            KeyValue("account_number", "XX12 3456 7890", "xx1234567890"),
            KeyValue("invoice_number", "FA-2024/0137", "FA-2024/0137"),
            KeyValue("amount_due", "999,00", "999.00"))))
        assert [(kv.label, kv.canonical) for kv in p.key_values] == [
            ("amount_total", "1250.00"), ("account_number", "xx1234567890"),
            ("invoice_number", "fa20240137"), ("amount_due", "999.00")]   # kept, as the model wrote it (Δ59)
        assert _grades(p, "value") == [("1.250,00 €", "in_text"), ("XX12 3456 7890", "in_text"),
                                       ("FA-2024/0137", "in_text"), ("999,00", "not_in_text")]
        assert counts.not_in_text == {"value": 1}

    def test_dates_only_when_their_iso_form_is_a_term(self):
        p, counts = self._verify(_profile(dates=(
            ProfileDate("issued", date(2024, 3, 15)), ProfileDate("valid_until", date(2024, 4, 30)),
            ProfileDate("due", date(2024, 5, 1)))))
        assert [d.iso for d in p.dates] == [date(2024, 3, 15), date(2024, 4, 30), date(2024, 5, 1)]   # kept (Δ59)
        assert _grades(p, "date") == [("2024-03-15", "in_text"), ("2024-04-30", "in_text"),
                                      ("2024-05-01", "not_in_text")]
        assert counts.not_in_text == {"date": 1}
        assert dp.valid_until_of(p) == date(2024, 4, 30) and dp.issued_of(p) == date(2024, 3, 15)

    def test_validity_words_need_a_verified_validity_or_due_date(self):
        p, _ = self._verify(_profile(validity_words=("valid until", "caduca"),
                                     dates=(ProfileDate("valid_until", date(2030, 1, 1)),)))
        assert p.validity_words == () and dp.valid_until_of(p) is None
        assert _grades(p, "date") == [("2030-01-01", "not_in_text")]   # kept, but never a validity date (Δ60)
        p, _ = self._verify(_profile(validity_words=("valid until",),
                                     dates=(ProfileDate("valid_until", date(2024, 4, 30)),)))
        assert p.validity_words == ("valid until",)

    def test_due_stands_in_for_valid_until(self):
        p, _ = self._verify(_profile(dates=(ProfileDate("due", date(2024, 4, 30)),)))
        assert dp.valid_until_of(p) == date(2024, 4, 30)

    def test_labels_outside_the_fixed_set(self):
        p, counts = self._verify(_profile(doc_type="spaceship", life_domains=("finance", "space", "finance", "home"),
                                         dates=(ProfileDate("printed", date(2024, 3, 15)),),
                                         entities=(ProfileEntity("Springfield", "city", "Springfield"),),
                                         key_values=(KeyValue("vat", "1.250,00", "1250.00"),)))
        assert p.doc_type == "other" and p.life_domains == ("finance", "home")
        assert p.dates[0].label == "other" and p.entities[0].type == "other"
        assert p.key_values[0].label == "other_id"
        assert counts.labels == 5
        assert set(p.life_domains) <= set(LIFE_DOMAINS)

    def test_no_key_values_for_kinds_that_get_none(self):
        p, counts = self._verify(_profile(key_values=(KeyValue("amount_total", "1.250,00 €", "1250.00"),)),
                                 key_values=False)
        # the kind gets none (§25.3): neither kept nor graded; it is not a value the text lacks (Δ59)
        assert p.key_values == () and _grades(p, "value") == [] and counts.not_in_text == {}

    def test_caps(self):
        many = tuple(ProfileEntity("Springfield", "place", "Springfield", tuple(f"f{i}" for i in range(20)))
                     for _ in range(30))
        words = tuple(f"w{i}" for i in range(50))
        p, _ = self._verify(_profile(entities=many, topic_words=words, label_words=("a", " a ", "", "b")))
        assert len(p.entities) == PROFILE_ENTITIES_MAX and len(p.entities[0].forms) == 8
        assert len(p.topic_words) == PROFILE_WORD_LIST_MAX and p.label_words == ("a", "b")

    def test_a_comma_joined_word_list_entry_becomes_one_entry_per_word(self):
        """Seen live (gpt-6-luna, 2026-10-13): 'electricity, electric, luz' as one entry of topic_words."""
        p, _ = self._verify(_profile(topic_words=("electricity, electric; luz", "electric"),
                                     label_words=("invoice,invoices",)))
        assert p.topic_words == ("electricity", "electric", "luz") and p.label_words == ("invoice", "invoices")
        assert p.topics == ()

    def test_dropping_everything_is_not_a_failure(self):
        # Δ59: nothing is dropped; a profile none of whose values the text holds is still a profile
        p, counts = self._verify(_profile(entities=(ProfileEntity("Oslo", "place", "Oslo"),),
                                          key_values=(KeyValue("price", "7", "7"),)))
        assert [e.name for e in p.entities] == ["Oslo"] and [kv.canonical for kv in p.key_values] == ["7"]
        assert _grades(p, "entity") == [("Oslo", "not_in_text")] and _grades(p, "value") == [("7", "not_in_text")]
        assert counts.not_in_text == {"entity": 1, "value": 1}
        assert p.what_it_is == "An invoice"
