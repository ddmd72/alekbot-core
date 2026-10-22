"""Two RFC §6 clauses of the Cabinet drive disclosure, each pinned by a fragment
only that clause contains.

The shared `DISCLOSURE_FRAGMENTS` pins the telemetry clause with "30 days", which the 30-day-keep clause
("about 30 days") also contains, and the excerpts-to-model clause with "AI model", which the model-provider
clause ("AI model provider") also contains. Dropping either clause left that test green.
"""
import pytest

from .test_cabinet_drive_index_strings import LANGS, _dicts

pytestmark = pytest.mark.datafile

# (telemetry: excerpts stay in operational logs for up to 30 days, excerpts reach the model)
CLAUSE_ONLY_FRAGMENTS = {
    "en": ("operational logs for up to 30 days", "Excerpts reach the AI model"),
    "uk": ("журналах до 30 днів", "Уривки потрапляють до ШІ-моделі"),
    "fr": ("jusqu'à 30 jours dans les journaux", "Des extraits parviennent au modèle d'IA"),
    "es": ("registros técnicos hasta 30 días", "Los fragmentos llegan al modelo de IA"),
}


@pytest.mark.parametrize("lang", LANGS)
def test_disclosure_has_the_telemetry_and_excerpts_to_model_clauses(lang):
    _, dicts = _dicts()
    text = dicts[lang]["drive.index_disclosure"]
    missing = [f for f in CLAUSE_ONLY_FRAGMENTS[lang] if f not in text]
    assert missing == []


@pytest.mark.parametrize("lang", LANGS)
def test_each_fragment_occurs_once(lang):
    """A fragment that occurs twice would not tell the two clauses apart."""
    _, dicts = _dicts()
    text = dicts[lang]["drive.index_disclosure"]
    assert [text.count(f) for f in CLAUSE_ONLY_FRAGMENTS[lang]] == [1, 1]
