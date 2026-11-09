"""The fact domain registry (fact profile RFC §3): 27 keys in the taxonomy's order, the enums derived from them, the
prompt renderers byte-equal to the reviewed texts, Lelik's default and the legacy configuration map."""
import hashlib
from dataclasses import FrozenInstanceError

import pytest

from src.domain import fact_domains as fd
from src.domain.entities import FactDomain
from src.domain.fact_domains import Domain

_ORDER = (
    "biography", "family", "people", "pets", "health", "sport", "food", "media", "hobbies", "going_out", "memories",
    "lifestyle", "beliefs", "home", "transport", "tech", "belongings", "appearance", "travel", "money", "savings",
    "state_law", "work", "projects", "learning", "abilities", "agent_directives",
)
_FLAG_DECIDED = {"biography", "family", "people", "pets", "state_law"}
# RFC §6 row 4
_VOICE_OPEN = {
    "biography", "food", "lifestyle", "abilities", "beliefs", "media", "hobbies", "sport", "going_out", "memories",
    "appearance", "belongings",
}
# sha256 of the reviewed texts: taxonomy §6 SUBJECT ENUM block and USE-AREA line, the P1 query prompt's LIFE AREAS block
_SUBJECT_BLOCK_SHA = "eb41a5d08bb2c1bc6e7601066b3bff6184b1e37ace7f9a5696100c0c0ebe5e63"
_USE_AREA_LINE_SHA = "6475975021827eecf431b9b6569233aa38469f09106f8290c214578c5dafc867"
_LIFE_AREAS_SHA = "24fea7afcefc6c549cde4fa1fa44fdae3ca578690a4da46aa60cf172bbcfe64f"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── Keys and entries ─────────────────────────────────────────────────────────

def test_27_unique_keys_in_the_taxonomy_order():
    assert fd.all_keys() == _ORDER
    assert len(set(fd.all_keys())) == 27


def test_every_domain_constant_is_an_entry_and_back():
    constants = {v for k, v in vars(Domain).items() if k.isupper()}
    assert constants == set(_ORDER)


def test_flag_decided_domains_have_no_subject_row():
    assert {e.key for e in fd.FACT_DOMAINS if e.subject_row is None} == _FLAG_DECIDED | {"agent_directives"}


def test_agent_directives_has_no_rows_and_no_area():
    e = fd.domain_entry(Domain.AGENT_DIRECTIVES)
    assert (e.query_row, e.subject_row, e.use_area, e.voice_open) == (None, None, False, False)


def test_every_other_domain_has_a_query_row():
    assert [e.key for e in fd.FACT_DOMAINS if not e.query_row] == ["agent_directives"]


def test_lookup_by_key():
    assert fd.domain_entry("travel").key == "travel"
    assert fd.domain_entry("biographical") is None
    assert fd.is_known("state_law") and not fd.is_known("none") and not fd.is_known("")


def test_cabinet_order_is_the_taxonomy_order():
    assert fd.cabinet_keys() == _ORDER
    assert len({e.cabinet_order for e in fd.FACT_DOMAINS}) == 27


def test_entries_are_immutable():
    e = fd.domain_entry("food")
    with pytest.raises(FrozenInstanceError):
        e.key = "drinks"  # type: ignore[misc]
    with pytest.raises(TypeError):
        e.labels["en"] = "Drinks"  # type: ignore[index]


# ── Derived enums ────────────────────────────────────────────────────────────

def test_subject_enum_is_21_domains_then_none_with_lifestyle_closing_the_domains():
    subjects = fd.subject_enum()
    assert len(subjects) == 22 and subjects[-1] == "none" and subjects[-2] == "lifestyle"
    assert set(subjects[:-1]) == set(_ORDER) - _FLAG_DECIDED - {"agent_directives"}


def test_use_area_enum_is_the_subject_enum_without_none_plus_four_flag_areas():
    areas = fd.use_area_enum()
    assert len(areas) == 25
    assert areas == fd.subject_enum()[:-1] + ("family", "people", "pets", "state_law")
    assert set(areas) == {e.key for e in fd.FACT_DOMAINS if e.use_area}


def test_voice_open_default_is_the_rfc_set_in_registry_order():
    assert set(fd.voice_open_default()) == _VOICE_OPEN
    assert list(fd.voice_open_default()) == [k for k in _ORDER if k in _VOICE_OPEN]


# ── Renderers ────────────────────────────────────────────────────────────────

def test_subject_enum_block_equals_the_reviewed_text():
    block = fd.render_subject_enum_block()
    assert _sha(block) == _SUBJECT_BLOCK_SHA
    lines = block.split("\n")
    rows = [line for line in lines[1:] if not line.startswith("  ")]
    assert lines[0] == "SUBJECT ENUM:"
    assert [r.split(": ", 1)[0] for r in rows] == list(fd.subject_enum())
    assert all(len(line) <= 118 for line in lines)


def test_use_area_line_equals_the_reviewed_text():
    line = fd.render_use_area_line()
    assert _sha(line) == _USE_AREA_LINE_SHA
    assert line.startswith("USE-AREA ENUM: the subject enum without none, plus family (a relative), ")
    assert line.count("\n") == 1 and line.endswith("state_law (the state or the law).")


def test_life_areas_block_equals_the_p1_prompt():
    block = fd.render_life_areas_block()
    assert _sha(block) == _LIFE_AREAS_SHA
    lines = block.split("\n")
    assert lines[0] == "LIFE AREAS" and len(lines) == 27
    assert [r.split(": ", 1)[0] for r in lines[1:]] == list(_ORDER[:-1])


# ── Labels ───────────────────────────────────────────────────────────────────

def test_label_in_the_locale():
    assert fd.label("health", "en") == "Health & wellbeing"
    assert fd.label("travel", "fr") == "Voyages"


def test_label_falls_back_to_english_for_an_unknown_locale():
    assert fd.label("going_out", "pt") == "Celebrations & going out"


def test_label_of_an_unknown_key_is_none():
    assert fd.label("biographical", "en") is None


# ── Legacy configuration map ─────────────────────────────────────────────────

@pytest.mark.parametrize("old", [d.value for d in FactDomain])
def test_every_old_value_maps_to_known_keys_only(old):
    keys = fd.config_key(old)
    assert keys and all(fd.is_known(k) for k in keys)
    assert len(set(keys)) == len(keys)
    assert list(keys) == [k for k in _ORDER if k in keys]


@pytest.mark.parametrize("key", _ORDER)
def test_a_new_key_maps_to_itself(key):
    assert fd.config_key(key) == (key,)


@pytest.mark.parametrize("value", ["", "none", "Health", "medical", "agent_directive "])
def test_an_unknown_value_maps_to_nothing(value):
    assert fd.config_key(value) == ()


def test_the_old_voice_default_maps_to_the_new_default():
    old_default = (FactDomain.BIOGRAPHICAL.value, FactDomain.PREFERENCE.value)
    mapped = {k for v in old_default for k in fd.config_key(v)}
    assert mapped == _VOICE_OPEN


def test_the_old_directive_value_maps_to_agent_directives_only():
    assert fd.config_key("agent_directive") == ("agent_directives",)


def test_legacy_values_are_only_old_domain_values():
    old = {d.value for d in FactDomain}
    assert {v for e in fd.FACT_DOMAINS for v in e.legacy_config_values} == old
