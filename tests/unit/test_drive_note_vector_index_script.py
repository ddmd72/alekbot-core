"""The owner-run script that creates the vector index of the owner notes (DRIVE_FILE_NOTES_RFC §3.5 item 6): the
index `DriveNoteRepository.find_nearest` needs — `note_vector` (768, flat) under the equality prefilters user_id, state
and embedding_space — on the live prefixed collection, and nothing hard-coded that belongs in the env file."""
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = _ROOT / "scripts" / "infrastructure" / "create_drive_note_vector_index.sh"


def _text():
    return SCRIPT.read_text()


def test_the_index_matches_find_nearest():
    text = _text()
    order = [line.strip() for line in text.splitlines() if line.strip().startswith("--field-config=")]
    assert order == [
        "--field-config=order=ASCENDING,field-path=user_id \\",
        "--field-config=order=ASCENDING,field-path=state \\",
        "--field-config=order=ASCENDING,field-path=embedding_space \\",
        '--field-config=vector-config="$VECTOR_CONFIG",field-path=note_vector \\',
    ]
    assert """VECTOR_CONFIG='{"dimension":"768", "flat": "{}"}'""" in text


def test_the_collection_and_the_project_come_from_the_environment():
    text = _text()
    assert 'COLLECTION="${COLLECTION_PREFIX-development_}drive_notes"' in text
    assert 'PROJECT_ID="${PROJECT_ID:?set PROJECT_ID' in text
    assert 'DATABASE_ID="us-production"' in text and "set -euo pipefail" in text


def test_the_find_nearest_prefilter_is_the_scripts():
    """The repository's query filters exactly the script's prefilter fields."""
    source = (_ROOT / "src" / "adapters" / "firestore_drive_note_repo.py").read_text()
    body = source[source.index("async def find_nearest"):source.index("# -- lifecycle")]
    assert 'FieldFilter("state", "==", state)' in body
    assert 'FieldFilter("embedding_space", "==", embedding_space)' in body
    assert "self._user(user_id)" in body
