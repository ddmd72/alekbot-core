"""The outbound-request guard's pure rules (EXTERNAL_TASK_QUEUE_RFC §5.8 "Outbound-request guard", §6.2, E4).

Each rule has its own test. Examples: Alex Doe's worker is woken at `hooks.example.com`; the bot's own host is
`svc.example.org`. Addresses come from the documentation and special-purpose registries, plus a few public ones.
"""
import pytest

from src.domain.external_task import WAKE_HEADER_BYTES_MAX, WAKE_HEADERS_MAX, WAKE_URL_MAX_CHARS
from src.domain.wake_refusal import WakeRefusal, WakeTargetRefused
from src.domain.wake_target import (
    CheckedTarget,
    WakeTarget,
    address_is_public,
    check_headers,
    check_target,
    check_url,
    own_hosts_of,
    parse_header_lines,
    wake_aad,
)

R = WakeRefusal
OWN = frozenset({"svc.example.org"})


def test_a_public_https_url_passes_with_its_host_and_request_target():
    assert check_url("https://Hooks.Example.com./wake?token=abc#frag", OWN) == CheckedTarget(
        host="hooks.example.com", request_target="/wake?token=abc")
    assert check_url("https://hooks.example.com", OWN) == CheckedTarget("hooks.example.com", "/")
    assert check_url("https://hooks.example.com:443/w", OWN) == CheckedTarget("hooks.example.com", "/w")


@pytest.mark.parametrize("url", ["http://hooks.example.com/", "ftp://hooks.example.com/", "hooks.example.com/w",
                                 "//hooks.example.com/w"])
def test_https_only(url):
    assert check_url(url, OWN) is R.SCHEME


@pytest.mark.parametrize("url", ["https://hooks.example.com:8443/", "https://hooks.example.com:80/",
                                 "https://hooks.example.com:99999/", "https://hooks.example.com:abc/"])
def test_port_443_only(url):
    assert check_url(url, OWN) is R.PORT


def test_no_userinfo():
    assert check_url("https://alex:pw@hooks.example.com/", OWN) is R.NAME
    assert check_url("https://svc.example.org@hooks.example.com/", OWN) is R.NAME


@pytest.mark.parametrize("url", ["https://203.0.113.7/", "https://8.8.8.8/w", "https://[2606:4700::1]/",
                                 "https://[::1]/", "https://127.1/", "https://0x7f.0.0.1/", "https://2130706433/"])
def test_no_ip_literal_or_numeric_host(url):
    assert check_url(url, OWN) is R.NAME


@pytest.mark.parametrize("url", ["https://localhost/", "https://metadata/", "https://hooks/", "https://-a.example.com/",
                                 "https://a_b.example.com/", "https://a..example.com/", "https://[::1/"])
def test_a_single_label_or_malformed_name(url):
    assert check_url(url, OWN) is R.NAME


@pytest.mark.parametrize("url", ["https://hooks.example.com/ x", "https://hooks.example.com/\nx", "https://hööks.example/",
                                 "", "https://" + "a" * WAKE_URL_MAX_CHARS + ".example.com/",
                                 "https://" + ".".join(["a" * 63] * 4) + ".com/"])
def test_whitespace_control_non_ascii_and_overlong_urls(url):
    assert check_url(url, OWN) is R.NAME


@pytest.mark.parametrize("url", ["https://metadata.google.internal/computeMetadata/v1/", "https://svc.internal/",
                                 "https://box.localhost/", "https://printer.local/", "https://1.0.0.127.in-addr.arpa/"])
def test_internal_names_are_refused_before_resolving(url):
    assert check_url(url, OWN) is R.INTERNAL_NAME


@pytest.mark.parametrize("url", ["https://svc.example.org/worker", "https://SVC.example.org./", "https://run.app/",
                                 "https://svc-abc123-uc.a.run.app/worker"])
def test_the_bots_own_hosts_and_the_platform_domain(url):
    assert check_url(url, OWN) is R.OWN_HOST


def test_a_name_that_only_contains_an_own_host_is_not_one():
    assert isinstance(check_url("https://svc.example.org.hooks.example.com/", OWN), CheckedTarget)


def test_headers_must_be_tokens_with_printable_values():
    assert check_headers((("Authorization", "Bearer abc"), ("X-Worker", "research\tlane"))) is None
    assert check_headers((("Bad Name", "v"),)) is R.HEADER
    assert check_headers((("X-A", "line\r\nInjected: 1"),)) is R.HEADER
    assert check_headers((("X-A", "naïve"),)) is R.HEADER
    assert check_headers((("X-A", 1),)) is R.HEADER


@pytest.mark.parametrize("name", ["Host", "content-length", "Transfer-Encoding", "CONNECTION", "Keep-Alive", "TE",
                                  "Upgrade", "Content-Type", "Trailer", "Proxy-Connection"])
def test_framing_and_hop_by_hop_headers_are_refused(name):
    assert check_headers(((name, "x"),)) is R.HEADER


def test_header_count_size_and_repeats_are_capped():
    assert check_headers(tuple((f"X-{i}", "v") for i in range(WAKE_HEADERS_MAX))) is None
    assert check_headers(tuple((f"X-{i}", "v") for i in range(WAKE_HEADERS_MAX + 1))) is R.HEADER
    assert check_headers((("X-A", "v" * (WAKE_HEADER_BYTES_MAX - 3)),)) is None
    assert check_headers((("X-A", "v" * (WAKE_HEADER_BYTES_MAX - 2)),)) is R.HEADER
    assert check_headers((("X-A", "1"), ("x-a", "2"))) is R.HEADER


def test_check_target_applies_the_url_rules_then_the_header_rules():
    assert check_target(WakeTarget("http://hooks.example.com/", (("Host", "x"),)), OWN) is R.SCHEME
    assert check_target(WakeTarget("https://hooks.example.com/", (("Host", "x"),)), OWN) is R.HEADER
    assert check_target(WakeTarget("https://hooks.example.com/w", (("X-A", "1"),)), OWN) == CheckedTarget(
        "hooks.example.com", "/w")


def test_header_lines_parse_into_pairs_and_a_bad_line_is_refused():
    assert parse_header_lines("Authorization: Bearer a:b\n\n  X-Lane :  research  \n") == (
        ("Authorization", "Bearer a:b"), ("X-Lane", "research"))
    assert parse_header_lines("") == ()
    assert parse_header_lines("no colon here") is R.HEADER
    assert parse_header_lines(": value") is R.HEADER
    assert parse_header_lines("Host: elsewhere.example") is R.HEADER
    assert parse_header_lines(None) is R.HEADER


@pytest.mark.parametrize("address", [
    "10.0.0.5", "172.16.0.1", "192.168.1.1",           # private
    "127.0.0.1", "::1",                                 # loopback
    "169.254.169.254", "fe80::1", "fe80::1%en0",        # link-local, the metadata address among them
    "100.64.0.1", "100.127.255.254",                    # CGNAT
    "224.0.0.1", "239.1.2.3", "ff02::1",                # multicast
    "240.0.0.1", "255.255.255.255",                     # reserved
    "0.0.0.0", "::",                                    # unspecified
    "192.0.2.1", "198.51.100.1", "203.0.113.1", "2001:db8::1",   # documentation
    "198.18.0.1",                                       # benchmarking
    "fc00::1", "fd12:3456::1",                          # IPv6 ULA
    "::ffff:10.0.0.1", "::ffff:169.254.169.254",        # IPv4-mapped, judged as IPv4
    "not-an-address",
])
def test_non_public_addresses_are_refused(address):
    assert address_is_public(address) is False


@pytest.mark.parametrize("address", ["64:ff9b::808:808", "64:ff9b:1::808:808", "2002:808:808::1",
                                     "2001:0:4136:e378:8000:63bf:3fff:fdd2"])
def test_addresses_that_embed_ipv4_are_refused_outright(address):
    assert address_is_public(address) is False


@pytest.mark.parametrize("address", ["8.8.8.8", "93.184.216.34", "2606:4700::6810:84e5", "::ffff:8.8.8.8"])
def test_global_unicast_addresses_pass(address):
    assert address_is_public(address) is True


def test_own_hosts_come_from_the_configured_urls():
    assert own_hosts_of(["https://SVC.example.org", "wss://relay.example.org/stream", None, "", "  ",
                         "https://cabinet.example.org./auth/callback", "https://[bad", "no-scheme"]) == frozenset(
        {"svc.example.org", "relay.example.org", "cabinet.example.org"})


def test_a_target_round_trips_through_its_stored_json_and_a_foreign_text_is_refused():
    target = WakeTarget("https://hooks.example.com/wake?t=1", (("Authorization", "Bearer abc"),))
    assert WakeTarget.from_json(target.to_json()) == target
    assert WakeTarget.from_json('{"url": "https://hooks.example.com/"}') == WakeTarget("https://hooks.example.com/")
    for text in ('[]', '{"url": 1}', '{"url": "u", "headers": {"a": "b"}}', '{"url": "u", "headers": [["a"]]}',
                 '{"url": "u", "headers": [["a", 2]]}'):
        with pytest.raises(ValueError):
            WakeTarget.from_json(text)


def test_the_aad_binds_the_ciphertext_to_the_worker_record():
    assert wake_aad("usr-1", "wrk-1") == "external_workers/usr-1:wrk-1"


def test_only_a_server_error_and_unreachable_are_retryable():
    assert {r for r in WakeRefusal if not r.is_final} == {R.SERVER_ERROR, R.UNREACHABLE}
    assert WakeTargetRefused(R.PORT).refusal is R.PORT and str(WakeTargetRefused(R.PORT)) == "port"
