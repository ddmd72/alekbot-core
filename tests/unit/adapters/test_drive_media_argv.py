"""The ffprobe / ffmpeg argv of the media child (USER_DRIVE_INDEXING_RFC §27.4, Δ99, Δ134; plan G14): every hardening
flag is pinned, so a refactor cannot drop one silently."""

import pytest

from src.adapters.drive_parser import media

URL = "http://127.0.0.1:40123/recording"           # the sandbox's loopback range server, the only input since S5
CA = ""


def _window(container="mov,mp4,m4a,3gp,3g2,mj2"):
    return media.build_window_argv(URL, CA, container, 120.0, 1800.0, "/w/window_2.opus.part")


def _value(argv, flag):
    return argv[argv.index(flag) + 1]


def test_protocol_whitelist_pinned():
    for argv in (_window(), media.build_probe_argv(URL, CA),
                 media.build_pass_argv(URL, CA, "matroska,webm", 0, 600, "/w/pass.opus")):
        assert _value(argv, "-protocol_whitelist") == "http,tcp"      # plain loopback http only: no `crypto` (Δ180, T-R32)


def test_format_whitelist_pinned():
    expected = "mov,mp4,m4a,matroska,webm,mp3,wav,flac,ogg,aac,avi,asf,aiff,amr,flv"   # the last five: T-R32
    assert _value(media.build_probe_argv(URL, CA), "-format_whitelist") == expected
    assert _value(_window(), "-format_whitelist") == expected
    assert _value(_window(), "-f") == "mov,mp4,m4a,3gp,3g2,mj2"       # the probed demuxer pinned on the extraction run
    assert "-f" not in media.build_probe_argv(URL, CA)


def test_a_container_name_cannot_inject_a_flag():
    with pytest.raises(ValueError):
        media.build_window_argv(URL, CA, "-i /etc/passwd", 0, 10, "/w/x")


def test_nostdin_and_no_filters():
    argv = _window()
    assert "-nostdin" in argv and argv[0] == "ffmpeg"
    assert {"-vf", "-af", "-filter_complex", "-filter", "-lavfi", "-f_lavfi"}.isdisjoint(argv)
    assert {"-hide_banner", "-nostats"} <= set(argv) and _value(argv, "-loglevel") == "error"
    assert "-nostdin" not in media.build_probe_argv(URL, CA)         # ffprobe has no such option; its stdin is closed


def test_audio_map_only():
    argv = _window()
    assert _value(argv, "-map") == "0:a:0" and argv.count("-map") == 1
    assert {"-vn", "-sn", "-dn"} <= set(argv)
    assert (_value(argv, "-ac"), _value(argv, "-ar"), _value(argv, "-c:a"), _value(argv, "-b:a")) == (
        "1", "16000", "libopus", "24k")
    assert argv[-1].endswith("window_2.opus.part") and _value(argv, "-f") != argv[-1]


def test_input_side_seek():
    argv = _window()
    i = argv.index("-i")
    assert argv.index("-ss") < i and argv.index("-t") < i        # before -i: a Range read, not a decode-and-discard
    assert argv[i + 1] == URL and _value(argv, "-ss") == "120.000" and _value(argv, "-t") == "1800.000"


def test_pass_argv_is_guarded_bounded_and_resumable():
    argv = media.build_pass_argv(URL, CA, "matroska,webm", 0, 5_400, "/w/pass.opus")
    assert "-ss" not in argv                                      # from the start: nothing to seek to
    assert _value(argv, "-t") == "5400.000" and argv.index("-t") < argv.index("-i")
    assert _value(argv, "-format_whitelist") == media.FORMAT_WHITELIST and _value(argv, "-f") == "matroska,webm"
    assert "-nostdin" in argv and argv[-1] == "/w/pass.opus" and argv[-2] == "opus"
    resumed = media.build_pass_argv(URL, CA, "matroska,webm", 3_480, 5_400, "/w/pass.opus")
    assert _value(resumed, "-ss") == "3480.000" and resumed.index("-ss") < resumed.index("-i")


def test_remote_runs_reconnect_and_fail_on_error():
    expected = ["-reconnect", "1", "-reconnect_on_network_error", "1", "-reconnect_delay_max", "5"]
    for argv in (_window(), media.build_pass_argv(URL, CA, "matroska,webm", 0, 600, "/w/pass.opus"),
                 media.build_probe_argv(URL, CA)):
        start = argv.index("-reconnect")
        assert argv[start:start + 6] == expected and start < argv.index("-i")
    for argv in (_window(), media.build_pass_argv(URL, CA, "matroska,webm", 0, 600, "/w/pass.opus")):
        assert "-xerror" in argv and argv.index("-xerror") < argv.index("-i")
    assert "-xerror" not in media.build_cut_argv("/w/p.opus", 0, 10, "/w/x")      # local cuts are not remote runs


def test_the_rerun_without_xerror_keeps_the_reconnect_and_every_guard():
    plain = media.build_window_argv(URL, CA, "matroska,webm", 0, 10, "/w/x", xerror=False)
    assert "-xerror" not in plain and "-reconnect" in plain
    assert _value(plain, "-protocol_whitelist") == "http,tcp" and _value(plain, "-format_whitelist") == media.FORMAT_WHITELIST
    assert _value(plain, "-rw_timeout") == str(media.NETWORK_TIMEOUT_US) and _value(plain, "-f") == "matroska,webm"
    assert "-xerror" not in media.build_pass_argv(URL, CA, "matroska,webm", 0, 10, "/w/x", xerror=False)


def test_the_cut_reads_only_local_ogg():
    argv = media.build_cut_argv("/w/pass.opus", 1_740.0, 1_800.0, "/w/window_1.opus.part")
    assert _value(argv, "-protocol_whitelist") == "file" and _value(argv, "-format_whitelist") == "ogg"
    assert _value(argv, "-c:a") == "copy" and _value(argv, "-f") == "ogg" and "http" not in " ".join(argv)


def test_vbr_is_constrained_so_four_hours_fit_the_file_limit():
    for argv in (_window(), media.build_pass_argv(URL, CA, "matroska,webm", 0, 600, "/w/pass.opus")):
        assert _value(argv, "-vbr") == "constrained"


def test_an_empty_or_invalid_container_is_refused():
    for bad in ("", "-i /etc/passwd", "a b"):
        with pytest.raises(ValueError):
            media.build_window_argv(URL, CA, bad, 0, 10, "/w/x")
        with pytest.raises(ValueError):
            media.build_pass_argv(URL, CA, bad, 0, 10, "/w/x")
