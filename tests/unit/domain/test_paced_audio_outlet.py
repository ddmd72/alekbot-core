from src.domain.paced_audio_outlet import PacedAudioOutlet


def test_idle_outlet_yields_silence_and_no_marks():
    outlet = PacedAudioOutlet(frame_bytes=4)
    assert outlet.next_frame() == (b"\x00" * 4, [])


def test_mark_is_released_only_when_its_chunk_is_fully_handed_on():
    outlet = PacedAudioOutlet(frame_bytes=4)
    outlet.push(b"abcdef", "6")
    assert outlet.next_frame() == (b"abcd", [])
    assert outlet.next_frame() == (b"ef\x00\x00", ["6"])  # tail padded, mark released
    assert outlet.next_frame() == (b"\x00" * 4, [])


def test_several_chunks_release_marks_in_order():
    outlet = PacedAudioOutlet(frame_bytes=4)
    outlet.push(b"ab", "2")
    outlet.push(b"cd", "4")
    outlet.push(b"ef", "6")
    assert outlet.next_frame() == (b"abcd", ["2", "4"])
    assert outlet.next_frame() == (b"ef\x00\x00", ["6"])


def test_convert_is_applied_before_framing():
    outlet = PacedAudioOutlet(frame_bytes=4, convert=lambda b: b * 2)
    outlet.push(b"ab", "2")
    assert outlet.next_frame() == (b"abab", ["2"])


def test_clear_drops_audio_and_marks():
    outlet = PacedAudioOutlet(frame_bytes=4)
    outlet.push(b"abcdefgh", "8")
    outlet.clear()
    assert outlet.pending_bytes == 0
    assert outlet.next_frame() == (b"\x00" * 4, [])
