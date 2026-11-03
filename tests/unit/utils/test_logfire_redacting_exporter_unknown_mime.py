"""RedactingExporter: a file part that names no mime type anywhere is still omitted, with a null mime_type."""
import json

from tests.unit.utils.test_logfire_redacting_exporter import IN, _export


def test_a_file_part_with_no_mime_type_is_omitted_with_a_null_mime():
    wire = json.dumps([{"role": "user", "parts": [{"type": "blob", "content": "opaque-bytes-not-a-data-uri"},
                                                  {"type": "text", "content": "page 3 of the lease"}]}])
    (span,) = _export({IN: wire})
    parts = json.loads(span.attributes[IN])[0]["parts"]
    assert parts[0] == {"type": "file_omitted", "orig_type": "blob", "mime_type": None}
    assert parts[1] == {"type": "text", "content": "page 3 of the lease"}
    assert "opaque-bytes" not in span.attributes[IN]
