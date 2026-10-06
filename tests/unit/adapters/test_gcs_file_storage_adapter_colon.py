"""
Unit test: sanitize_filename maps ':' to '_' (RFC §15.4).

A user-uploaded file named with a colon must never collide with the `skill:<name>/<path>`
ref prefix once it is addressed by filename — if ':' survived sanitization, an upload
named "skill:x" would be indistinguishable from a skill file ref.
"""

from src.adapters.gcs_file_storage_adapter import sanitize_filename


def test_colon_mapped():
    assert sanitize_filename("skill:x/y.md") == "skill_x/y.md"


def test_colon_mapped_mid_filename():
    assert sanitize_filename("report:final.txt") == "report_final.txt"
