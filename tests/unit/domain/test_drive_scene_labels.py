"""Scene labels (owner decision D40, 2026-10-19): the fixed SCENE_LABELS, their names in en, es, uk and ru (SCENE_NAMES,
pinned by a hash), keyed `sl:` for a photograph only (automatic, Labelled), a query word emitting `sl:` only for a form
a name is stored under, the labels' English names in the card text and a `scene:` line on the rendered card."""
import pytest

from src.domain.drive_card import card_text
from src.domain.drive_index import KEYS_VERSION, DriveIndexRecord, RecordState, Scope, TriageVerdict
from src.domain.drive_key_sources import AboutField, RecordKeyInputs
from src.domain.drive_keys import (
    about_keys_of, index_tables_digest, name_keys_v2, query_concepts, source_class_of_about_key,
)
from src.domain.drive_labels import SCENE_LABELS, SCENE_NAMES, scene_names_digest
from src.domain.drive_match import FAMILY_SOURCE_CLASS, KeyFamily, MatchTier, SourceClass
from src.domain.drive_search import DriveSearchCard, _card_body, make_card
from src.domain.drive_tier_explain import explain
from src.domain.drive_tiers import Allowance, FileKeys, classify

PHOTO, TEXT = TriageVerdict.PHOTO, TriageVerdict.TEXT
SCENE_PINS = {4: "83c086f8e668dc1120b837ea319afa1e68220961fbb39a6c5d8245404476e142"}


def _photo(*labels, verdict=PHOTO):
    return RecordKeyInputs(kind="image", triage_verdict=verdict, photo_scene_labels=tuple(labels))


def _covers(query, rec):
    keys = set(about_keys_of(rec))
    return all(set(c.about_keys) & keys for c in query_concepts(query).concepts)


def _classify(query, rec):
    f = FileKeys("f", frozenset(about_keys_of(rec)), frozenset(name_keys_v2("x.jpg")), {}, {})
    return classify(f, query_concepts(query).concepts, Allowance(), {}, {})


class TestTable:
    def test_the_label_set_is_exactly_the_thirty_two(self):
        assert SCENE_LABELS == (
            "sunrise", "sunset", "night", "golden_hour", "rain", "snow", "fog", "sunny", "storm", "beach", "sea",
            "mountains", "forest", "lake_river", "city_street", "countryside", "indoors", "restaurant_cafe", "home",
            "party", "wedding", "birthday", "concert", "sport", "travel", "selfie", "group", "child", "pet", "food",
            "landscape", "architecture")
        assert len(SCENE_LABELS) == 32 and len(set(SCENE_LABELS)) == 32

    def test_every_label_has_names_and_no_name_is_foreign(self):
        assert set(SCENE_NAMES) == set(SCENE_LABELS)
        assert all(SCENE_NAMES[label] for label in SCENE_LABELS)

    @pytest.mark.parametrize("label,words", [
        ("sunset", ("sunset", "sunsets", "puesta de sol", "atardecer", "захід сонця", "закат", "закаты")),
        ("beach", ("beach", "playa", "пляж")),
        ("birthday", ("birthday", "cumpleaños", "день народження", "день рождения")),
    ])
    def test_natural_words_in_four_languages(self, label, words):
        assert set(words) <= set(SCENE_NAMES[label])

    def test_the_table_is_pinned_to_the_keys_version(self):
        # a change to SCENE_NAMES is a KEYS_VERSION bump: pin the new digest beside the old one
        assert SCENE_PINS.get(KEYS_VERSION) == scene_names_digest(), scene_names_digest()

    def test_the_index_tables_digest_covers_the_scene_table(self, monkeypatch):
        import src.domain.drive_keys as keys

        before = index_tables_digest()
        monkeypatch.setattr(keys, "scene_names_digest", lambda: "changed")
        assert index_tables_digest() != before


class TestKeys:
    def test_a_photograph_gets_sl_keys_and_they_are_automatic(self):
        keys = [k for k in about_keys_of(_photo("sunset")) if k.startswith("sl:")]
        assert "sl:sunset" in keys and "sl:закат" in keys
        assert KeyFamily.SCENE_LABEL.value == "sl:"
        assert FAMILY_SOURCE_CLASS[KeyFamily.SCENE_LABEL] is SourceClass.AUTOMATIC_RECORD
        assert all(source_class_of_about_key(k) is SourceClass.AUTOMATIC_RECORD for k in keys)

    def test_a_multi_word_name_is_one_phrase_key(self):
        assert any(k.startswith("sl:") and "_" in k for k in about_keys_of(_photo("sunset")))

    def test_a_text_image_and_a_document_get_none(self):
        assert not [k for k in about_keys_of(_photo("sunset", verdict=TEXT)) if k.startswith("sl:")]
        doc = RecordKeyInputs(kind="pdf", photo_scene_labels=("sunset",))
        assert not [k for k in about_keys_of(doc) if k.startswith("sl:")]

    def test_a_photograph_without_labels_gets_none(self):
        assert not [k for k in about_keys_of(_photo()) if k.startswith("sl:")]

    @pytest.mark.parametrize("query", ["закаты", "puesta de sol", "sunsets", "захід сонця", "atardecer", "sunset"])
    def test_a_sunset_query_emits_sl_sunset_and_covers_the_photo(self, query):
        emitted = {k for c in query_concepts(query).concepts for k in c.about_keys if k.startswith("sl:")}
        assert emitted, query
        assert _covers(query, _photo("sunset"))

    def test_a_scene_word_does_not_find_another_scene(self):
        assert not _covers("sunsets", _photo("sunrise"))

    @pytest.mark.parametrize("query", ["Lisbon", "contract 2024", "invoice", "Torre de Belém"])
    def test_an_ordinary_query_emits_no_sl_key(self, query):
        assert not [k for c in query_concepts(query).concepts for k in c.about_keys if k.startswith("sl:")]

    def test_a_model_said_it_so_the_tier_is_labelled(self):
        match = _classify("sunsets", _photo("sunset"))
        assert match is not None and match.tier is MatchTier.LABELLED

    def test_the_cap_is_six_labels(self):
        keys = about_keys_of(_photo(*SCENE_LABELS[:10]))
        capped = about_keys_of(_photo(*SCENE_LABELS[:6]))
        assert [k for k in keys if k.startswith("sl:")] == [k for k in capped if k.startswith("sl:")]


class TestExplain:
    def test_a_match_through_sl_is_shown_automatic(self):
        rec = _photo("sunset")
        match = _classify("закаты", rec)
        assert explain(match, rec, "x.jpg", None, ()).sources == ('scene "закаты" (automatic)',)


def _rec(**kw):
    base = dict(user_id="u", item_id="x", name="IMG_1.jpg", kind="image", scope=Scope.NORMAL,
                state=RecordState.METADATA_ONLY, triage_verdict=PHOTO)
    base.update(kw)
    return DriveIndexRecord(**base)


class TestCard:
    def test_the_card_text_has_the_english_names_after_the_scene(self):
        text = card_text(_rec(photo_description="people on a sand shore", photo_scene="shore at dusk",
                              photo_scene_labels=("sunset", "lake_river")))
        assert text == "IMG_1.jpg | image | people on a sand shore | shore at dusk | sunset, lake river"

    def test_no_labels_leave_the_card_text_as_it_was(self):
        assert card_text(_rec(photo_description="d", photo_scene="s")) == "IMG_1.jpg | image | d | s"

    def test_the_labels_change_the_card_hash(self):
        from src.domain.drive_card import card_hash

        assert card_hash(_rec(photo_scene_labels=("sea",))) != card_hash(_rec())

    def test_a_rendered_card_shows_a_scene_line(self):
        card = make_card(_rec(photo_scene_labels=("sunset", "beach")), "/P/IMG_1.jpg", "IMG_1.jpg | image", "abcdefgh", "a1")
        assert isinstance(card, DriveSearchCard) and card.scene_labels == ("sunset", "beach")
        assert "scene: sunset, beach" in _card_body(card)

    def test_the_tiered_renderer_shows_it_too(self):
        from src.domain.drive_tier_render import card_body

        card = make_card(_rec(photo_scene_labels=("sunset", "beach")), "/P/IMG_1.jpg", "IMG_1.jpg | image", "abcdefgh", "a1")
        assert "scene: sunset, beach" in card_body(card)

    def test_a_card_without_labels_has_no_scene_line(self):
        card = make_card(_rec(), "/P/IMG_1.jpg", "IMG_1.jpg | image", "abcdefgh", "a1")
        assert "scene:" not in _card_body(card)

    def test_the_field_is_the_source_of_the_line(self):
        assert AboutField.SCENE_LABEL.value == "scene_label"
