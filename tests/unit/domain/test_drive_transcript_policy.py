"""Policy 3 (USER_DRIVE_INDEXING_RFC §27.3, §27.14 T1): audio and video are admitted for transcription, read at call
time like the other two sets."""
import pytest

from src.domain import drive_index_policy as policy
from src.domain.drive_index import CURRENT_VERSIONS, POLICY_VERSION
from src.domain.drive_index_policy import TRANSCRIBED_KINDS, Admission, admission


@pytest.mark.parametrize("kind", ["audio", "video"])
def test_audio_and_video_admit_transcribe(kind):
    assert admission(kind) is Admission.TRANSCRIBE
    assert TRANSCRIBED_KINDS == frozenset({"audio", "video"})


def test_other_media_kinds_unchanged():
    assert admission("image") is Admission.TRIAGE
    assert admission("pdf") is Admission.CONTENT
    for kind in ("other", "folder", "doc", "code"):
        assert admission(kind) is Admission.NONE


def test_admission_reads_sets_at_call_time(monkeypatch):
    monkeypatch.setattr(policy, "TRANSCRIBED_KINDS", frozenset({"audio"}))
    assert admission("audio") is Admission.TRANSCRIBE
    assert admission("video") is Admission.NONE


def test_policy_is_four_and_card_version_is_untouched():
    # 3 admitted audio and video (§27.3); 4 reads `.epub.pkgf` packages as epub (§5.1)
    assert POLICY_VERSION == 4 and CURRENT_VERSIONS["policy"] == 4
    assert "render" not in CURRENT_VERSIONS and "summary" not in CURRENT_VERSIONS
