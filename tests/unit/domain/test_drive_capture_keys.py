"""Capture keys (USER_DRIVE_INDEXING_RFC §26.3, Δ58): the fixed CAPTURE_NAMES table, pinned by its hash like D31's
tables; `cp:` when code detected the capture (the verified-record class, About) and `cpa:` when a model said it (the
automatic-record class, Labelled); a query word emits both, as it emits `k:`; `digital` has no words; photographs get
none; and the owner's case: "скани" meets `cp:скан`."""
import pytest

from src.domain.drive_index import KEYS_VERSION, TriageVerdict
from src.domain.drive_key_sources import AboutField, RecordKeyInputs
from src.domain.drive_keys import (
    about_keys_of, about_sources, index_tables_digest, query_concepts, source_class_of_about_key,
)
from src.domain.drive_labels import CAPTURE_NAMES, capture_names_digest
from src.domain.drive_match import FAMILY_SOURCE_CLASS, KeyFamily, MatchTier, SourceClass
from src.domain.drive_tiers import Allowance, FileKeys, classify


def _rec(capture, source, kind="pdf"):
    return RecordKeyInputs(kind=kind, capture=capture, capture_source=source)


def _covers(query, rec):
    keys = set(about_keys_of(rec))
    return all(set(c.about_keys) & keys for c in query_concepts(query).concepts)


def _tier(query, rec):
    f = FileKeys(item_id="f", about_keys=frozenset(about_keys_of(rec)), name_keys=frozenset(), chunk_keys={},
                 hint_keys={})
    match = classify(f, query_concepts(query).concepts, Allowance(), {}, {})
    return match.tier if match is not None else None


class TestTable:
    def test_the_capture_values_and_digital_has_no_words(self):
        assert set(CAPTURE_NAMES) == {"scan", "document_photo", "screenshot", "digital"}
        assert CAPTURE_NAMES["digital"] == ()

    @pytest.mark.parametrize("capture,words", [
        ("scan", ("scan", "scanned", "escaneo", "escaneado", "скан", "скани", "сканкопія", "скан-копія", "сканкопия",
                  "скан-копия")),
        ("document_photo", ("foto de documento", "фото документа")),
        ("screenshot", ("screenshot", "captura de pantalla", "скріншот", "скриншот", "знімок екрана")),
    ])
    def test_the_rfc_words_are_in_the_table(self, capture, words):
        assert set(words) <= set(CAPTURE_NAMES[capture])

    def test_the_table_is_pinned_to_the_keys_version(self):
        # a change to CAPTURE_NAMES is a KEYS_VERSION bump: pin the new digest beside the old one
        assert CAPTURE_PINS.get(KEYS_VERSION) == capture_names_digest(), capture_names_digest()

    def test_the_index_tables_digest_covers_the_capture_table(self, monkeypatch):
        import src.domain.drive_keys as keys

        before = index_tables_digest()
        monkeypatch.setattr(keys, "capture_names_digest", lambda: "changed")
        assert index_tables_digest() != before

    def test_keys_version_is_4(self):
        assert KEYS_VERSION == 4


CAPTURE_PINS = {4: "db47e6be29b2b5ab84679eaf5c4d2bd873e98319502e3439d340dd35e0b5b3d4"}   # KEYS_VERSION -> capture_names_digest()


class TestFamilies:
    def test_cp_is_verified_and_cpa_automatic(self):
        assert KeyFamily.CAPTURE.value == "cp:" and KeyFamily.CAPTURE_AUTO.value == "cpa:"
        assert FAMILY_SOURCE_CLASS[KeyFamily.CAPTURE] is SourceClass.VERIFIED_RECORD
        assert FAMILY_SOURCE_CLASS[KeyFamily.CAPTURE_AUTO] is SourceClass.AUTOMATIC_RECORD
        assert source_class_of_about_key("cp:скан") is SourceClass.VERIFIED_RECORD
        assert source_class_of_about_key("cpa:скан") is SourceClass.AUTOMATIC_RECORD

    def test_a_detected_scan_is_keyed_cp_only(self):
        keys = about_keys_of(_rec("scan", "detected"))
        assert "cp:скан" in keys and "cp:scan" in keys and not any(k.startswith("cpa:") for k in keys)

    def test_a_model_scan_is_keyed_cpa_only(self):
        keys = about_keys_of(_rec("scan", "automatic"))
        assert "cpa:скан" in keys and not any(k.startswith("cp:") for k in keys)

    def test_digital_and_no_capture_have_no_capture_keys(self):
        for rec in (_rec("digital", "detected"), _rec("digital", "automatic"), _rec("", "")):
            assert not any(k.startswith(("cp:", "cpa:")) for k in about_keys_of(rec))

    def test_a_photograph_gets_none_even_with_a_stray_capture(self):
        rec = RecordKeyInputs(kind="image", triage_verdict=TriageVerdict.PHOTO, capture="scan",
                              capture_source="detected")
        assert not any(k.startswith(("cp:", "cpa:")) for k in about_keys_of(rec))

    def test_the_source_names_the_capture(self):
        sources = about_sources(_rec("scan", "detected"))["cp:скан"]
        assert sources[0].field is AboutField.CAPTURE and sources[0].source_class is SourceClass.VERIFIED_RECORD

    def test_a_query_word_emits_both_families(self):
        about = query_concepts("скани").concepts[0].about_keys
        assert "cp:скан" in about and "cpa:скан" in about

    def test_a_query_phrase_run_emits_both(self):
        concept = query_concepts("фото документа").concepts[0]
        assert "cp:фот_документ" in concept.about_keys and "cpa:фот_документ" in concept.about_keys


class TestSearch:
    def test_skany_meets_cp_skan(self):
        # §26.8: the owner's query word meets a detected scan, whatever the file's name
        assert _covers("скани", _rec("scan", "detected"))
        assert _tier("скани", _rec("scan", "detected")) is MatchTier.ABOUT

    def test_a_model_said_scan_is_labelled(self):
        assert _tier("скани", _rec("scan", "automatic")) is MatchTier.LABELLED

    @pytest.mark.parametrize("query", ["scans", "scanned", "escaneados", "сканы", "скан-копія", "сканкопии"])
    def test_scan_words(self, query):
        assert _covers(query, _rec("scan", "detected")), query

    @pytest.mark.parametrize("query", ["screenshots", "скріншоти", "скриншоты", "знімок екрана", "captura de pantalla"])
    def test_screenshot_words(self, query):
        assert _covers(query, _rec("screenshot", "automatic", kind="image")), query

    @pytest.mark.parametrize("query", ["фото документа", "фото документов", "document photo", "foto de documento"])
    def test_document_photo_words(self, query):
        assert _covers(query, _rec("document_photo", "detected", kind="image")), query

    def test_a_scan_is_not_found_as_a_screenshot(self):
        assert not _covers("скриншот", _rec("scan", "detected"))
