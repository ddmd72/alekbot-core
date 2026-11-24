"""Cabinet -> Settings -> Connections -> External workers: a worker's OAuth grants (EXTERNAL_TASK_QUEUE_RFC §5.4
"OAuth", §5.6) are listed under its keys with the client name, the granted time and the last refresh, each with a
Revoke; the strings are in all four languages. Runs the row's script under node against the fake DOM of
`test_cabinet_external_workers_page.py`. The routes: `test_cabinet_external_worker_grants.py`."""
import json

from tests.unit.web.test_cabinet_external_workers_page import LANGS, _WORKER, _WORKERS_JS, _dict, _node, _run

GRANT_KEYS = ("workers.grants_title", "workers.grant_granted", "workers.grant_refreshed", "workers.grant_unknown",
              "workers.revoke_grant", "workers.revoke_grant_confirm", "workers.grant_revoked")
_GRANT = {"grant_id": "g-1", "client_name": "Claude", "granted_at": "2026-11-21T09:00:00+00:00",
          "last_refresh": "2026-11-22T09:00:00+00:00"}


def test_every_grant_string_is_in_all_four_languages_and_used_by_the_row():
    dicts = {lang: _dict(lang) for lang in LANGS}
    for key in GRANT_KEYS:
        assert f't("{key}")' in _WORKERS_JS, key
        for lang in LANGS:
            assert dicts[lang].get(key), (lang, key)


@_node
def test_a_workers_grants_are_listed_with_their_times_and_a_revoke():
    worker = dict(_WORKER, grants=[_GRANT, dict(_GRANT, grant_id="g-2", client_name="Inspector", granted_at=None,
                                                 last_refresh=None)])
    out = _run(f"""renderExternalWorkers({{enabled: true, workers: [{json.dumps(worker)}]}});
      return els["workers-list"].innerHTML;""")
    en = _dict("en")
    assert en["workers.grants_title"] in out and "Claude" in out and "Inspector" in out
    assert 'data-grant="g-1"' in out and 'data-grant="g-2"' in out and out.count(en["workers.revoke_grant"]) == 2
    assert en["workers.grant_unknown"] in out


@_node
def test_a_worker_without_grants_shows_no_grant_section():
    out = _run(f"""renderExternalWorkers({{enabled: true, workers: [{json.dumps(dict(_WORKER, grants=[]))},
        {json.dumps(_WORKER)}]}});
      return els["workers-list"].innerHTML;""")
    assert _dict("en")["workers.grants_title"] not in out


@_node
def test_a_client_name_is_escaped():
    worker = dict(_WORKER, grants=[dict(_GRANT, client_name="<img src=x onerror=alert(1)>")])
    out = _run(f"""renderExternalWorkers({{enabled: true, workers: [{json.dumps(worker)}]}});
      return els["workers-list"].innerHTML;""")
    assert "<img" not in out and "&#60;img" in out


@_node
def test_revoke_asks_first_then_deletes_the_grant_and_reloads():
    out = _run("""answer = false; await revokeWorkerGrant("wrk-1", "g-1"); const declined = calls.length;
      answer = true;
      replies = [{status: 200, body: {revoked: true}}, {status: 200, body: {enabled: true, workers: []}}];
      await revokeWorkerGrant("wrk-1", "g/1");
      replies = [{status: 404, body: {error: "not_found"}}];
      await revokeWorkerGrant("wrk-1", "g-9");
      return [declined, calls.map((c) => c.method + " " + c.url), toasts];""")
    en = _dict("en")
    assert out[0] == 0
    assert out[1] == ["DELETE /api/external-workers/wrk-1/grants/g%2F1", "GET /api/external-workers",
                      "DELETE /api/external-workers/wrk-1/grants/g-9"]
    assert out[2] == [[en["workers.grant_revoked"], "success"], [en["workers.err_failed"], "error"]]
