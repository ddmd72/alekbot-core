"""The Cabinet's connect links and connect toasts come from `GET /api/connectors` (MULTI_DRIVE_RFC §5.2, slice 1a):
the Gmail, drive and To Do cards build `/auth/connect/<kind>` (Reconnect of a stored credential:
`?credential=<id>`) from the connector of their integration, and the callback's `?connected=<kind>` /
`?connect_error=<status>&kind=<kind>` toasts are named by the connector's label (Gmail and Tasks keep their scope
texts, every integration its localised "connected" text). The static checks read the page; the behavioural ones run
the helpers under node when node is installed."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_HTML = (Path(__file__).resolve().parents[3] / "src" / "web" / "static" / "cabinet.html").read_text(encoding="utf-8")
CONNECTORS = [
    {"kind": "gmail", "integration": "mail", "label": "Gmail"},
    {"kind": "microsoft_todo", "integration": "tasks", "label": "Microsoft To Do"},
    {"kind": "microsoft_onedrive", "integration": "drive", "label": "OneDrive"},
]


def _helpers() -> str:
    block = re.search(r"/\* ── Connectors.*?(?=\n      async function loadGmailStatus)", _HTML, re.S)
    escape = re.search(r"\n(\s*function escapeHtml\(str\) \{.*?\n\s*\})\n", _HTML, re.S)
    assert block and escape
    return block.group(0) + "\n" + escape.group(1)


def _run(body: str, connectors=CONNECTORS):
    script = (
        'const API = ""; const toasts = []; let fetches = 0;\n'
        'function t(key) { return "T:" + key; }\n'
        'function showToast(msg, type) { toasts.push([msg, type]); }\n'
        f'const CONNECTORS = {json.dumps(connectors)};\n'
        'globalThis.fetch = async (url) => { fetches += 1; if (url !== "/api/connectors") throw new Error(url);\n'
        '  return { ok: true, json: async () => ({ connectors: CONNECTORS }) }; };\n'
        + _helpers() + "\n(async () => {\n" + body + "\n})().catch((e) => { console.error(e); process.exit(1); });\n")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout
    return json.loads(out)


def test_no_card_names_a_connect_path_of_its_own():
    assert "/auth/connect-" not in _HTML
    for card in ("renderGmailStatus", "renderDriveStatus", "renderServiceStatus"):
        body = re.search(rf"function {card}\(.*?\n      }}\n", _HTML, re.S).group(0)
        assert "connectButton(data.connect_href" in body, card
    for loader, integration in (("loadGmailStatus", "mail"), ("loadDriveStatus", "drive"), ("loadTasksStatus", "tasks")):
        body = re.search(rf"async function {loader}\(\) \{{.*?\n      }}\n", _HTML, re.S).group(0)
        assert f'connectHref("{integration}")' in body, loader
    assert 'connectHref("mail", data.credential_id)' in _HTML and 'connectHref("drive", data.credential_id)' in _HTML


def test_the_old_callback_params_are_gone_and_the_new_ones_are_read():
    for old in ("gmail_connected", "gmail_error", "tasks_error", "microsoft_todo_connected", "drive_error"):
        assert f'qs.get("{old}")' not in _HTML, old
    assert "await showConnectToasts(qs);" in _HTML


node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


@node
def test_the_links_are_built_from_the_connector_of_the_integration():
    out = _run('process.stdout.write(JSON.stringify([await connectHref("mail"), await connectHref("drive", "cred 1/x"),'
               ' await connectHref("tasks"), await connectHref("calendar"), connectButton("", "Connect"),'
               ' connectButton("/auth/connect/gmail", "Connect"), fetches]));')
    assert out == ["/auth/connect/gmail", "/auth/connect/microsoft_onedrive?credential=cred%201%2Fx",
                   "/auth/connect/microsoft_todo", "", "",
                   '<a href="/auth/connect/gmail" class="btn btn-black">Connect</a>', 1]   # one read, memoised


@node
@pytest.mark.parametrize("query, toast", [
    ("connected=gmail", ["T:toast.gmail_connected", "success"]),
    ("connected=microsoft_todo", ["T:toast.todo_connected", "success"]),
    ("connected=microsoft_onedrive", ["T:toast.drive_connected", "success"]),
    ("connect_error=scope&kind=gmail",
     ["Google did not grant access to your mail — reconnect and tick the Gmail checkbox", "error"]),
    ("connect_error=scope&kind=microsoft_onedrive",
     ["OneDrive did not get the access it needs — reconnect and allow every permission asked", "error"]),
    ("connect_error=account&kind=gmail",
     ["Gmail: you signed in with another account — reconnect with the account this connection uses", "error"]),
    ("connect_error=exchange&kind=microsoft_todo", ["Microsoft To Do connection error", "error"]),
    ("connect_error=denied", ["Integration connection error", "error"]),
])
def test_the_callback_toast_is_named_by_the_connector(query, toast):
    out = _run(f'await showConnectToasts(new URLSearchParams("{query}"));'
               ' process.stdout.write(JSON.stringify(toasts));')
    assert out == [toast]


@node
def test_no_callback_param_means_no_toast_and_no_read():
    assert _run('await showConnectToasts(new URLSearchParams("tab=integrations"));'
                ' process.stdout.write(JSON.stringify([toasts, fetches]));') == [[], 0]
