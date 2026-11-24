"""Cabinet -> External workers -> wake-up form with a pasted `curl` command and a separate secret. The logic runs under
node against the fake DOM of `test_cabinet_external_workers_page.py`; the strings are read as text. The form opens empty;
the command is sent as typed and the secret trimmed, in a body of `{curl, secret}`.
"""
from src.domain.wake_refusal import WakeRefusal
from tests.unit.web.test_cabinet_external_workers_page import LANGS, _SHELL, _dict, _node, _run

_CURL_REFUSALS = ("syntax", "option", "method", "url", "variables", "secret_missing", "secret_unused", "secret")
_OLD_KEYS = ("wake_url", "wake_url_placeholder", "wake_headers", "wake_headers_placeholder")


def test_every_command_refusal_has_its_message_in_all_four_languages_and_the_old_form_keys_are_gone():
    assert {f"curl_{c}" for c in _CURL_REFUSALS} <= {r.value for r in WakeRefusal}
    for lang in LANGS:
        strings = _dict(lang)
        for code in _CURL_REFUSALS:
            assert strings.get(f"workers.err_wake_curl_{code}"), (lang, code)
        for key in ("wake_curl", "wake_curl_placeholder", "wake_secret", "wake_secret_placeholder", "wake_hint"):
            assert strings.get(f"workers.{key}"), (lang, key)
        for key in _OLD_KEYS:
            assert f"workers.{key}" not in strings, (lang, key)
        assert "$TOKEN" in strings["workers.wake_hint"] and "hooks.example.com" in strings["workers.wake_curl_placeholder"]


@_node
def test_the_form_has_a_command_box_and_a_masked_secret_and_sends_both():
    out = _run("""openWorkerWake("wrk-1", "research");
      const form = els["workers-wake-box"].innerHTML;
      els["workers-wake-curl"].value = "curl -X POST https://hooks.example.com/wake -H 'Authorization: Bearer $TOKEN'";
      els["workers-wake-secret"].value = "  s3cret-token ";
      replies = [{status: 200, body: {wake_host: "hooks.example.com"}}, {status: 200, body: {enabled: false}}];
      await saveWorkerWake("wrk-1");
      return [form, calls, els["workers-wake-box"].innerHTML, toasts];""")
    form, calls, after, toasts = out
    assert 'id="workers-wake-curl"' in form and 'id="workers-wake-secret"' in form and 'type="password"' in form
    assert "workers-wake-url" not in form and "workers-wake-headers" not in form and "s3cret" not in form
    assert calls[0] == {"url": "/api/external-workers/wrk-1/wake", "method": "PUT",
                        "body": {"curl": "curl -X POST https://hooks.example.com/wake -H 'Authorization: Bearer $TOKEN'",
                                 "secret": "s3cret-token"}}
    assert after == "" and toasts == [[_dict("en")["workers.wake_saved"], "success"]]


@_node
def test_a_refused_command_keeps_the_form_and_names_the_code():
    out = _run("""openWorkerWake("wrk-1", "research");
      replies = [{status: 400, body: {error: "wake_curl_secret_missing"}}];
      await saveWorkerWake("wrk-1");
      return [toasts, els["workers-wake-box"].innerHTML !== ""];""")
    assert out == [[[_dict("en")["workers.err_wake_curl_secret_missing"], "error"]], True]
