"""Cabinet -> Settings -> Connections -> External workers: the wake-up part of a worker's card (EXTERNAL_TASK_QUEUE_RFC
§5.6, §5.8). The logic runs under node against the fake DOM of `test_cabinet_external_workers_page.py`; the strings are
read as text. The form always opens empty and is dropped when the row closes; after a save the page shows the host only.
Examples: Alex Doe's worker `research`, woken at `hooks.example.com`.
"""
import json
import re

from src.domain.wake_refusal import WakeRefusal
from tests.unit.web.test_cabinet_external_workers_page import LANGS, _SHELL, _WORKER, _dict, _node, _run

_SAVE_REFUSALS = ("scheme", "port", "name", "internal_name", "own_host", "header", "unavailable")


def test_every_code_a_save_can_answer_has_its_message_in_all_four_languages():
    assert {f"wake_{r.value}" for r in WakeRefusal} >= {f"wake_{c}" for c in _SAVE_REFUSALS if c != "unavailable"}
    for lang in LANGS:
        strings = _dict(lang)
        for code in _SAVE_REFUSALS:
            assert strings.get(f"workers.err_wake_{code}"), (lang, code)


def test_the_wake_box_sits_in_the_row_next_to_the_key_box():
    start = _SHELL.index('id="srow-workers-body"')
    body = _SHELL[start:_SHELL.index('id="srow-drives"')]
    assert body.index('id="workers-key-box"') < body.index('id="workers-wake-box"') < body.index('id="workers-list"')


@_node
def test_a_card_shows_the_host_escaped_or_that_the_worker_polls():
    worker = dict(_WORKER, wake_host="hooks.example.com<b>")
    out = _run(f"""renderExternalWorkers({{enabled: true, workers: [{json.dumps(worker)}, {json.dumps(_WORKER)}]}});
      return els["workers-list"].innerHTML;""")
    en = _dict("en")
    assert "hooks.example.com&#60;b&#62;" in out and "<b>" not in out
    assert en["workers.wake_polls"] in out and en["workers.wake_clear"] in out and en["workers.wake_set"] in out


@_node
def test_the_form_opens_empty_saves_the_command_and_the_trimmed_secret_and_closes():
    command = "curl -X POST https://hooks.example.com/wake \\\n  -H 'Authorization: Bearer $TOKEN'"
    out = _run(f"""openWorkerWake("wrk-1", "research");
      const form = els["workers-wake-box"].innerHTML;
      els["workers-wake-curl"].value = {json.dumps(command)};
      els["workers-wake-secret"].value = "  abc  ";
      replies = [{{status: 200, body: {{wake_host: "hooks.example.com"}}}}, {{status: 200, body: {{enabled: false}}}}];
      await saveWorkerWake("wrk-1");
      return [form, calls, els["workers-wake-box"].innerHTML, toasts];""")
    form, calls, after, toasts = out
    assert 'id="workers-wake-curl"' in form and 'id="workers-wake-secret"' in form and "research" in form
    assert "workers-wake-url" not in form and "workers-wake-headers" not in form
    secret_input = re.search(r'<input id="workers-wake-secret"[^>]*>', form).group(0)
    assert 'type="password"' in secret_input and 'autocomplete="off"' in secret_input
    assert "value=" not in form and "abc" not in form
    assert calls[0] == {"url": "/api/external-workers/wrk-1/wake", "method": "PUT",
                        "body": {"curl": command, "secret": "abc"}}
    assert calls[1]["url"] == "/api/external-workers" and after == ""
    assert toasts == [[_dict("en")["workers.wake_saved"], "success"]]


@_node
def test_a_refused_save_keeps_the_form_and_names_the_code():
    out = _run("""openWorkerWake("wrk-1", "research");
      replies = [{status: 400, body: {error: "wake_internal_name"}}];
      await saveWorkerWake("wrk-1");
      return [toasts, els["workers-wake-box"].innerHTML !== ""];""")
    assert out == [[[_dict("en")["workers.err_wake_internal_name"], "error"]], True]


@_node
def test_clearing_asks_first_then_deletes_the_target():
    out = _run("""answer = false; await clearWorkerWake("wrk-1"); const asked = calls.length;
      answer = true; replies = [{status: 200, body: {wake_host: null}}, {status: 200, body: {enabled: false}}];
      await clearWorkerWake("wrk-1");
      return [asked, calls.map((c) => c.method + " " + c.url), toasts];""")
    assert out[0] == 0
    assert out[1] == ["DELETE /api/external-workers/wrk-1/wake", "GET /api/external-workers"]
    assert out[2] == [[_dict("en")["workers.wake_cleared"], "success"]]


@_node
def test_closing_the_row_or_losing_the_flag_drops_a_typed_target():
    out = _run("""renderExternalWorkers({enabled: true, workers: []}); openSettingsRow("workers");
      openWorkerWake("wrk-1", "research"); openSettingsRow("tz"); const closed = els["workers-wake-box"].innerHTML;
      openSettingsRow("workers"); openWorkerWake("wrk-1", "research"); renderExternalWorkers({enabled: false});
      return [closed, els["workers-wake-box"].innerHTML];""")
    assert out == ["", ""]

