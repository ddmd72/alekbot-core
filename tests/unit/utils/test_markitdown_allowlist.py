"""The child's markitdown converters stay inside the Phase 0 allowlist (`document_hardening`); the unsafe builtins
(zip, audio, image, URL converters) are never on it."""
from src.adapters.drive_parser.readers import MARKITDOWN_CONVERTERS
from src.utils.document_hardening import MARKITDOWN_ALLOWED_CONVERTERS


def test_the_childs_converters_are_a_subset_of_the_allowlist():
    used = {class_name for class_name, _ext, _mime in MARKITDOWN_CONVERTERS.values()}
    assert used and used <= MARKITDOWN_ALLOWED_CONVERTERS


def test_the_allowlist_excludes_the_unsafe_converters():
    for unsafe in ("ZipConverter", "AudioConverter", "ImageConverter", "YouTubeConverter",
                   "WikipediaConverter", "BingSerpConverter", "DocumentIntelligenceConverter"):
        assert unsafe not in MARKITDOWN_ALLOWED_CONVERTERS
