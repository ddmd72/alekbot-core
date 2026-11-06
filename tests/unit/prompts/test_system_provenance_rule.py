"""Structure of the system-turn provenance rule (SYSTEM_TURN_FINGERPRINT_RFC §4.1): token, blueprint class, profiles.
The wording is tunable and pinned by no test."""
import re

from src.domain.prompt_v3.blueprint import Blueprint
from src.domain.system_fingerprint import PROVENANCE_CLASS, WORDS
from tests.support import prompt_vocabulary as pv

TOKEN_ID = "POLICY_SYSTEM_PROVENANCE"
DOC = pv.ser.token_from_file(pv.token_path(TOKEN_ID).read_text(encoding="utf-8"))


def _class_of(token_id):
    """The class of the token file, wherever the snapshot keeps it (system/ or user/); None if no file."""
    for scope in ("system", "user"):
        path = pv.SNAPSHOT / "tokens" / scope / f"{token_id}.groovy"
        if path.exists():
            return pv.ser.token_from_file(path.read_text(encoding="utf-8"))["class"]
    return None


def _profiles_with_tokens():
    for path in sorted((pv.SNAPSHOT / "profiles").glob("*.yaml")):
        profile = pv.load_profile(path.stem)
        if isinstance(profile.get("tokens"), dict):
            yield path.stem, profile


def test_token_file_parses_with_the_provenance_class():
    assert DOC["class"] == PROVENANCE_CLASS
    assert DOC["category"] == PROVENANCE_CLASS
    assert DOC["token_id"] == pv.token_path(TOKEN_ID).stem


def test_blueprint_lists_the_class_first_and_validates():
    doc = pv.load_blueprint("universal_agent_v1")
    assert doc["class_order"][0] == PROVENANCE_CLASS
    Blueprint(id=doc["blueprint_id"], outer_class=doc["outer_class"], class_order=list(doc["class_order"])).validate()


def test_only_smart_and_quick_assign_a_provenance_token():
    assigning = {}
    for agent_id, profile in _profiles_with_tokens():
        for token_id, entry in profile["tokens"].items():
            if _class_of(token_id) == PROVENANCE_CLASS:
                assigning.setdefault(agent_id, []).append(entry)
    assert set(assigning) == {"smart", "quick"}
    for entries in assigning.values():
        assert all(entry.get("non_overridable") is True for entry in entries)


def test_smart_and_quick_fit_their_blueprint():
    for agent_id in ("smart", "quick"):
        profile = pv.load_profile(agent_id)
        class_order = pv.load_blueprint(profile["blueprint_id"])["class_order"]
        for token_id in profile["tokens"]:
            klass = _class_of(token_id)
            assert klass is not None, (agent_id, token_id, "no token file")
            assert klass in class_order, (agent_id, token_id, klass)


def test_body_names_the_block_and_tag_but_carries_no_key_value():
    body = DOC["content"]
    assert "session_key" in body
    assert "system_note" in body
    word = "|".join(sorted(WORDS))
    assert not re.search(rf"\b(?:{word})-(?:{word})-(?:{word})\b", body)
