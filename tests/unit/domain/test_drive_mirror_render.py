import sys

import pytest

from src.domain import drive_mirror_render
from src.domain.drive_index import DriveIndexRecord
from src.domain.drive_index_params import ARCHIVE_KEYS_MAX
from src.domain.drive_mirror_render import (
    MirrorStrings, archive_word_keys, readme_bytes, render_file, rendered_hash,
)
from src.domain.drive_transcript import Segment, StitchedTranscript, StoredTranscript
from src.domain.drive_transcript_render import open_text

EN = MirrorStrings(
    open_recording="open the recording",
    managed_line="Managed by the bot: this file may be rewritten. Automatic transcript, may contain errors.",
    duration_minutes="{n} min", speakers_one="{n} speaker", speakers_many="{n} speakers",
    stub_no_speech="No speech was detected in this recording ({duration}).",
    stub_too_long="This recording is too long to transcribe ({duration}).",
    stub_too_large="This recording is too large to transcribe.",
    stub_no_audio="This recording has no audio track.",
    readme="# Transcripts\n\nManaged by the bot. Transcripts of deleted recordings are in Archive/.",
)
UK = MirrorStrings(
    open_recording="відкрити запис",
    managed_line="Керується ботом: файл може бути переписаний. Автоматична розшифровка, можливі помилки.",
    duration_minutes="{n} хв", speakers_one="{n} мовець", speakers_many="{n} мовців",
    stub_no_speech="У записі не знайдено мовлення ({duration}).",
    stub_too_long="Запис задовгий для розшифровки ({duration}).",
    stub_too_large="Запис завеликий для розшифровки.",
    stub_no_audio="У записі немає звукової доріжки.",
    readme="# Розшифровки\n\nКерується ботом. Розшифровки видалених записів лежать в Archive/.",
)


def _rec(**kw):
    base = dict(user_id="u", item_id="i1", parent_id="p1", name="Interview.mp4", web_url="https://x/1",
                transcript_state="current", transcript_seconds=2820, transcript_speakers=2,
                transcript_languages=("uk", "en"))
    base.update(kw)
    return DriveIndexRecord(**base)


def _stored(segments=None, **kw):
    segs = segments or (Segment(0.0, 1, "hello there"), Segment(65.0, 2, "general kenobi"))
    return StoredTranscript("k", StitchedTranscript(segs, ("uk", "en"), 2, 2820.0, **kw))


def _render(stored=None, rec=None, strings=EN, path="Videos/2024", on="2026-10-01"):
    return render_file(stored or _stored(), rec or _rec(), strings, source_path=path, recorded_on=on).decode("utf-8")


def test_render_header_fields_en_and_uk():
    en = _render().split("\n")
    assert en[0] == "# Interview.mp4"
    assert en[1] == "Videos/2024 · [open the recording](https://x/1)"
    assert en[2] == "2026-10-01 · 47 min · 2 speakers · uk/en"
    assert en[3] == EN.managed_line and en[4] == "" and en[5] == "---" and en[6] == ""
    uk = _render(strings=UK).split("\n")
    assert uk[1].endswith("[відкрити запис](https://x/1)")
    assert uk[2] == "2026-10-01 · 47 хв · 2 мовців · uk/en" and uk[3] == UK.managed_line


def test_single_speaker_and_missing_parts_are_left_out():
    text = _render(rec=_rec(transcript_speakers=1, transcript_languages=(), web_url=""), on="")
    lines = text.split("\n")
    assert lines[1] == "Videos/2024" and lines[2] == "47 min · 1 speaker"
    bare = _render(rec=_rec(transcript_seconds=0, transcript_speakers=0, transcript_languages=(), web_url=""), on="")
    assert bare.split("\n")[1] == "Videos/2024" and bare.split("\n")[2] == EN.managed_line


def test_render_escapes_markdown_in_names():
    rec = _rec(name="a_b*c [x](y) #1 <i>\nz")
    text = _render(rec=rec, path="R&D/_old*/[a]")
    lines = text.split("\n")
    assert lines[0] == "# a\\_b\\*c \\[x\\]\\(y\\) \\#1 \\<i\\> z"
    assert lines[1].startswith("R&D/\\_old\\*/\\[a\\] ")


def test_link_cannot_break_out_of_the_markdown_link():
    text = _render(rec=_rec(web_url="https://x/a b)c"))
    assert "(https://x/a%20b%29c)" in text


def test_body_equals_uncapped_open_text():
    stored = _stored()
    assert _render(stored).endswith("---\n\n" + open_text(stored, max_chars=sys.maxsize) + "\n")
    big = _stored(tuple(Segment(float(i), 1, "word " * 400) for i in range(200)), truncated=True, uncertain_seam=True)
    text = _render(big)
    body = open_text(big, max_chars=sys.maxsize)
    assert len(body) > 60_000 and "Transcript cut" not in text
    assert text.endswith(body + "\n")
    assert "part of this recording may be missing" in text and "speaker labels may differ" in text


@pytest.mark.parametrize("stub,fragment", [
    ("no_speech", "No speech was detected in this recording (47:00)."),
    ("too_long", "too long to transcribe (47:00)."),
    ("too_large", "too large to transcribe."),
    ("no_audio", "no audio track."),
])
def test_stub_file_per_stub(stub, fragment):
    text = _render(StoredTranscript("k", None, stub=stub), _rec(transcript_stub=stub, transcript_speakers=0,
                                                                  transcript_languages=()))
    head, body = text.split("\n---\n\n")
    assert body.strip().endswith(fragment) and body.count("\n") == 1
    assert head.startswith("# Interview.mp4\n") and EN.managed_line in head


def test_stub_takes_its_kind_from_the_record_when_the_store_lost_it():
    text = _render(StoredTranscript("k", None, stub=""), _rec(transcript_stub="no_audio"))
    assert text.endswith("no audio track.\n")


def test_hash_is_sha256_of_the_bytes_and_changes_with_the_text():
    a = render_file(_stored(), _rec(), EN, source_path="p", recorded_on="d")
    b = render_file(_stored(), _rec(name="Other.mp4"), EN, source_path="p", recorded_on="d")
    assert len(rendered_hash(a)) == 64 and rendered_hash(a) == rendered_hash(a) and rendered_hash(a) != rendered_hash(b)
    assert b"\r" not in a and a.endswith(b"\n")


def test_readme_text_per_locale_mentions_archive():
    for strings in (EN, UK):
        data = readme_bytes(strings)
        assert b"Archive/" in data and data.endswith(b"\n") and not data.endswith(b"\n\n")
    assert b"\r" not in readme_bytes(EN)


def _word(n):
    letters = ""
    while True:
        letters = chr(97 + n % 26) + letters
        n //= 26
        if n == 0:
            return "qz" + letters + "vx"


def test_archive_keys_per_segment_reach_past_1500():
    segs = tuple(Segment(float(i), 1, " ".join(_word(i * 20 + j) for j in range(20))) for i in range(150))
    keys = archive_word_keys(_stored(segs))
    assert len(keys) > 1500 and f"w:{_word(2999)}" in keys


def test_archive_keys_capped_first_seen(monkeypatch):
    monkeypatch.setattr(drive_mirror_render, "ARCHIVE_KEYS_MAX", 5)
    stored = _stored((Segment(0.0, 1, "alpha bravo"), Segment(1.0, 1, "alpha charlie delta")))
    keys = archive_word_keys(stored)
    assert len(keys) == 5 and len(set(keys)) == 5
    assert keys[0] == "w:alpha" and keys == archive_word_keys(stored)
    assert ARCHIVE_KEYS_MAX == 12_000


def test_archive_keys_union_deduplicates_in_first_seen_order():
    keys = archive_word_keys(_stored((Segment(0.0, 1, "alpha bravo"), Segment(1.0, 1, "bravo alpha charlie"))))
    assert len(keys) == len(set(keys)) and keys.index("w:alpha") < keys.index("w:bravo") < keys.index("w:charlie")


def test_archive_keys_skip_gap_segments():
    keys = archive_word_keys(_stored((Segment(0.0, 1, "alpha"), Segment(1.0, None, "bravo", gap=True))))
    assert "w:alpha" in keys and "w:bravo" not in keys


def test_archive_keys_empty_for_a_stub():
    assert archive_word_keys(StoredTranscript("k", None, stub="no_speech")) == ()
