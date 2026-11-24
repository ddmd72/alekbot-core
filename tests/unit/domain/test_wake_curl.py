"""The Cabinet's pasted wake-up command (`src/domain/wake_curl.py`): the URL and header pairs a `curl` command holds, the
secret put where its `$NAME` placeholder stands. Hosts are under example.com, secrets are made up.
"""
import pytest

from src.domain.wake_curl import COMMAND_MAX_CHARS, parse_wake_curl
from src.domain.wake_refusal import WakeRefusal as R

PROVIDER_STYLE = """curl -X POST https://hooks.example.com/wake/abc \\
  -H "Authorization: Bearer $TOKEN" \\
  -H "Content-Type: application/json" \\
  -H "X-Lane: research" \\
  -d '{"text": "hello", "n": 1}'
"""


def test_a_multi_line_provider_command_gives_the_url_and_two_headers_with_the_secret_in():
    assert parse_wake_curl(PROVIDER_STYLE, "s3cret-token") == (
        "https://hooks.example.com/wake/abc",
        (("Authorization", "Bearer s3cret-token"), ("X-Lane", "research")))


def test_the_braced_variable_form_and_a_secret_in_the_url():
    command = 'curl https://hooks.example.com/wake?key=${KEY} -H "X-Token: ${KEY}"'
    assert parse_wake_curl(command, "  s3cret-token \n") == (
        "https://hooks.example.com/wake?key=s3cret-token", (("X-Token", "s3cret-token"),))


def test_a_command_that_holds_its_secret_needs_an_empty_secret_field():
    command = "curl -X post https://hooks.example.com/wake -H 'Authorization: Bearer s3cret-token'"
    assert parse_wake_curl(command, "") == (
        "https://hooks.example.com/wake", (("Authorization", "Bearer s3cret-token"),))


def test_a_dollar_that_is_not_a_variable_stays_and_a_secret_is_substituted_literally():
    command = 'curl https://hooks.example.com/wake -H "X-A: $1" -H "X-B: $TOKEN-$TOKEN"'
    assert parse_wake_curl(command, "a\\1&b") == (
        "https://hooks.example.com/wake", (("X-A", "$1"), ("X-B", "a\\1&b-a\\1&b")))


def test_equals_attached_and_ignored_forms():
    command = ("curl -sS --fail -i -v --silent --show-error --request=POST --url=https://hooks.example.com/wake "
               "--header='X-One: 1' -H'X-Two: 2' -XPOST --json '{}' --data-raw x --data-binary y --data z -dq")
    assert parse_wake_curl(command, "") == ("https://hooks.example.com/wake", (("X-One", "1"), ("X-Two", "2")))


def test_the_url_may_follow_the_options_and_come_by_the_long_url_option():
    assert parse_wake_curl("curl -H 'X-A: 1' https://hooks.example.com/", "")[0] == "https://hooks.example.com/"
    assert parse_wake_curl("curl --url https://hooks.example.com/ -X POST", "")[0] == "https://hooks.example.com/"


def test_the_content_type_is_dropped_in_any_case():
    assert parse_wake_curl('curl https://hooks.example.com/ -H "cOnTeNt-TyPe: text/plain"', "")[1] == ()


@pytest.mark.parametrize("option", ["-k", "--insecure", "-u user:pw", "--proxy http://p.example.com", "-x http://p.example.com",
                                    "--resolve a.example.com:443:1.2.3.4", "--connect-to a:1:b:2", "-L", "-o out.txt",
                                    "-F a=b", "--cacert ca.pem", "-sk", "--", "-", "--data-urlencode a=b", "--compressed"])
def test_an_unknown_option_is_refused_never_skipped(option):
    assert parse_wake_curl(f"curl {option} https://hooks.example.com/wake", "") is R.CURL_OPTION


@pytest.mark.parametrize("command, code", [
    ("curl -X PUT https://hooks.example.com/", R.CURL_METHOD),
    ("curl --request GET https://hooks.example.com/", R.CURL_METHOD),
    ("curl -X POST", R.CURL_URL),
    ("curl https://a.example.com/ https://b.example.com/", R.CURL_URL),
    ("curl --url https://a.example.com/ https://b.example.com/", R.CURL_URL),
    ("curl --url= -X POST", R.CURL_URL),
    ("wget https://hooks.example.com/", R.CURL_SYNTAX),
    ("https://hooks.example.com/", R.CURL_SYNTAX),
    ("", R.CURL_SYNTAX),
    ("   \n", R.CURL_SYNTAX),
    ('curl https://hooks.example.com/ -H "X-A: 1', R.CURL_SYNTAX),
    ("curl https://hooks.example.com/ -H", R.CURL_SYNTAX),
    ("curl https://hooks.example.com/ --header", R.CURL_SYNTAX),
    ("curl https://hooks.example.com/ -X", R.CURL_SYNTAX),
    ("curl https://hooks.example.com/ -H 'no colon here'", R.HEADER),
    ("curl https://hooks.example.com/ -H ': value'", R.HEADER),
    ("curl https://hooks.example.com/ -H 'Host: other.example.com'", R.HEADER),
    ("curl https://hooks.example.com/ -H 'Accept-Encoding: gzip'", R.HEADER),
    ("curl https://hooks.example.com/ -H 'X-A: 1' -H 'x-a: 2'", R.HEADER),
])
def test_a_command_that_cannot_be_read_is_refused_with_its_code(command, code):
    assert parse_wake_curl(command, "") is code


def test_a_command_over_the_cap_or_not_text_is_refused():
    assert parse_wake_curl("curl https://hooks.example.com/ " + "-s " * COMMAND_MAX_CHARS, "") is R.CURL_SYNTAX
    assert parse_wake_curl(None, "") is R.CURL_SYNTAX      # type: ignore[arg-type]
    assert parse_wake_curl("curl https://hooks.example.com/", None) is R.CURL_SYNTAX      # type: ignore[arg-type]


def test_two_variable_names_are_refused_even_in_different_places():
    command = 'curl https://hooks.example.com/?a=$ONE -H "X-A: $TWO"'
    assert parse_wake_curl(command, "s3cret-token") is R.CURL_VARIABLES


def test_a_variable_needs_a_secret_and_a_secret_needs_a_variable():
    assert parse_wake_curl('curl https://hooks.example.com/ -H "X-A: $TOKEN"', "  ") is R.CURL_SECRET_MISSING
    assert parse_wake_curl("curl https://hooks.example.com/", "s3cret-token") is R.CURL_SECRET_UNUSED


@pytest.mark.parametrize("secret", ["bad\r\nX-Evil: 1", "line\nbreak", "in ner", "tab\tbed", "café", "nul\x00l"])
def test_a_secret_that_is_not_printable_ascii_without_spaces_is_refused(secret):
    assert parse_wake_curl('curl https://hooks.example.com/ -H "X-A: $TOKEN"', secret) is R.CURL_SECRET


def test_a_variable_in_the_data_is_not_a_placeholder():
    assert parse_wake_curl("curl https://hooks.example.com/ -d '{\"a\": \"$TOKEN\"}'", "") == (
        "https://hooks.example.com/", ())
