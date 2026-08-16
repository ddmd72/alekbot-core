"""Slack file translation: which uploads count as speech.

Slack names voice memos `audio_message.*` and marks the file `slack_audio`. The mimetype is
deliberately NOT the marker — the same memo has been observed as `audio/mp4` and `video/mp4`,
and a mimetype check silently loses the second form.
"""
from __future__ import annotations

from src.adapters.slack.http_adapter import HTTPModeAdapter


def _translate(files: list) -> list:
    # _translate_files touches no instance state, so it is exercised unbound — constructing
    # the adapter would drag in Slack Bolt, Firestore ports and a conversation handler.
    return HTTPModeAdapter._translate_files(None, files)


def _file(**overrides) -> dict:
    base = {
        "url_private": "https://files.slack.com/files-pri/T1-F1/x",
        "mimetype": "audio/mp4",
        "name": "audio_message.m4a",
        "size": 38_000,
    }
    base.update(overrides)
    return base


class TestSlackVoiceMarker:

    def test_audio_message_name_marks_a_voice_memo(self):
        [attachment] = _translate([_file()])
        assert attachment.is_voice_message is True

    def test_slack_audio_subtype_marks_a_voice_memo(self):
        [attachment] = _translate([_file(name="whatever.m4a", subtype="slack_audio")])
        assert attachment.is_voice_message is True

    def test_video_mp4_voice_memo_is_still_a_voice_memo(self):
        """The mimetype varies; the Slack-generated name does not."""
        [attachment] = _translate([_file(mimetype="video/mp4", name="audio_message.mp4")])
        assert attachment.is_voice_message is True

    def test_uploaded_audio_file_is_not_a_voice_memo(self):
        [attachment] = _translate([_file(name="podcast.mp3", mimetype="audio/mpeg")])
        assert attachment.is_voice_message is False

    def test_document_is_not_a_voice_memo(self):
        [attachment] = _translate([_file(name="report.pdf", mimetype="application/pdf")])
        assert attachment.is_voice_message is False

    def test_other_fields_still_translated(self):
        [attachment] = _translate([_file()])
        assert attachment.filename == "audio_message.m4a"
        assert attachment.mime_type == "audio/mp4"
        assert attachment.size_bytes == 38_000

    def test_file_without_url_is_skipped(self):
        assert _translate([_file(url_private=None)]) == []
