"""The delegate tool builds ONE `context` schema from every descriptor and the first registration wins
(base_agent.py, agent_registry.py), so a context field declared by several intents must say the same thing in each."""
from collections import defaultdict

from src.infrastructure.agent_manifest import ALL_DESCRIPTORS, FILE_REF_DESCRIPTION, Intent


# Known divergence that predates this test (image: 1k/2k, video: 480p-1080p). Shrink this set, never grow it.
_KNOWN_DIVERGENT = {"resolution"}


def _declarations():
    by_field = defaultdict(list)
    for descriptor in ALL_DESCRIPTORS:
        for intent, schema in descriptor.context_schemas.items():
            for field_name, spec in schema.items():
                text = spec if isinstance(spec, str) else spec.get("description", "")
                by_field[field_name].append((intent, text))
    return by_field


def test_every_shared_context_field_has_one_description():
    for field_name, declared in _declarations().items():
        if field_name in _KNOWN_DIVERGENT:
            continue
        texts = {text for _, text in declared}
        assert len(texts) == 1, f"{field_name} is described differently by {[i for i, _ in declared]}"


def test_file_ref_is_neutral_and_declared_by_the_opening_intents():
    declared = dict(_declarations()["file_ref"])
    assert {Intent.OPEN_FILE, Intent.OPEN_EMAIL_ATTACHMENT} <= set(declared)
    assert set(declared.values()) == {FILE_REF_DESCRIPTION}
    assert "email:" in FILE_REF_DESCRIPTION and "[File:" in FILE_REF_DESCRIPTION


def test_name_is_scoped_to_the_email_intent():
    name = dict(_declarations()["name"])
    assert name[Intent.OPEN_EMAIL_ATTACHMENT].startswith("open_email_attachment only:")
