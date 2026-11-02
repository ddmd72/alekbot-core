"""The ffmpeg/ffprobe argv by URL scheme (GOOGLE_DRIVE_RFC G2): the relay's loopback url gets `http,tcp` and no TLS
flags; an `https://` url keeps TLS verification; any other url is refused."""
import pytest

from src.adapters.drive_parser import media

CA = "/x/ca.pem"
RELAY = "http://127.0.0.1:41234/nonce-abc"
HTTPS = "https://media.example.invalid/Hamburg%20talk.mp4"


def _whitelist(argv):
    return argv[argv.index("-protocol_whitelist") + 1]


def _builders(url):
    return [media.build_probe_argv(url, CA),
            media.build_window_argv(url, CA, "matroska,webm", 0.0, 10.0, "/tmp/w.opus"),
            media.build_pass_argv(url, CA, "matroska,webm", 0.0, 10.0, "/tmp/w.ogg")]


def test_the_relay_url_gets_http_tcp_and_no_tls_flags():
    for argv in _builders(RELAY):
        assert _whitelist(argv) == "http,tcp"
        assert "-tls_verify" not in argv and "-ca_file" not in argv
        assert argv[argv.index("-format_whitelist") + 1] == media.FORMAT_WHITELIST
        assert RELAY in argv


def test_an_https_url_keeps_todays_flags():
    for argv in _builders(HTTPS):
        assert _whitelist(argv) == media.PROTOCOL_WHITELIST
        assert argv[argv.index("-tls_verify") + 1] == "1" and argv[argv.index("-ca_file") + 1] == CA


@pytest.mark.parametrize("url", ["http://media.example.invalid/x", "http://127.0.0.2:80/x", "file:///etc/passwd",
                                 "http://localhost:80/x", "ftp://127.0.0.1/x", "127.0.0.1:80/x", ""])
def test_any_other_url_is_a_value_error(url):
    for build in (lambda: media.build_probe_argv(url, CA),
                  lambda: media.build_window_argv(url, CA, "matroska,webm", 0.0, 10.0, "/tmp/w.opus"),
                  lambda: media.build_pass_argv(url, CA, "matroska,webm", 0.0, 10.0, "/tmp/w.ogg")):
        with pytest.raises(ValueError):
            build()
