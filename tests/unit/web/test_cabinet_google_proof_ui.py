"""Cabinet -> Devices: "Confirm with Google" after a failed passkey step-up (CABINET_DEVICE_SESSIONS_RFC §7.3). The page
shows the fallback only when a passkey change needed proof the step-up could not give, and its link goes to
`/auth/login/google` with `next` back to the Devices tab. Run under node when installed."""
import json
import re
import shutil
import subprocess
from urllib.parse import parse_qs, urlparse

import pytest

from tests.unit.web.cabinet_source import cabinet_source

_HTML = cabinet_source()
_NODE = shutil.which("node") is None
_KEYS = ["passkeys.google_proof_text", "passkeys.google_proof"]


def _function(name: str) -> str:
    match = re.search(r"\n(\s*(?:async )?function " + name + r"\(.*?\n      \})\n", _HTML, re.S)
    assert match, name
    return match.group(1)


@pytest.mark.parametrize("key", _KEYS)
def test_every_string_is_in_all_four_languages(key):
    assert len(re.findall(r'"' + re.escape(key) + r'": "[^"]+"', _HTML)) == 4


def test_the_fallback_starts_hidden_and_is_translated():
    assert 'id="passkeys-google-proof" class="hidden"' in _HTML
    for element in ("passkeys-google-proof-text", "passkeys-google-proof-link"):
        assert f'id="{element}"' in _HTML and f'"#{element}"' in _HTML


_SCRIPT = """
  const els = {{ "passkeys-google-proof": {{ hidden: true, classList: {{ remove: () => {{ els["passkeys-google-proof"].hidden = false; }} }} }},
                 "passkeys-google-proof-link": {{ href: "" }},
                 "passkey-offer": {{ classList: {{ add: () => {{}} }} }},
                 "section-devices": {{ classList: {{ contains: () => false }} }} }};
  const document = {{ getElementById: (id) => els[id] }};
  const t = (k) => k;
  const showToast = () => {{}};
  const rememberPasskeyOffer = () => {{}};
  const loadPasskeys = () => {{}};
  const window = {{ alekSession: {{ ensurePasskey: async () => {step_up},
                                  needsPasskey: async () => true }} }};
  const API = "";
  const authFetch = async () => ({{ ok: false, clone: () => ({{}}) }});
  const addPasskey = async () => {first};
  const confirm = () => true;
  {functions}
  Promise.resolve({call}).then((outcome) => process.stdout.write(JSON.stringify(
    {{ outcome, shown: !els["passkeys-google-proof"].hidden, href: els["passkeys-google-proof-link"].href }})));
"""


def _run(step_up: str, first: str, call: str = "addPasskeyHere()") -> dict:
    functions = "\n".join(_function(n) for n in ("googleProofUrl", "showGoogleProof", "addPasskeyHere", "removePasskey"))
    script = _SCRIPT.format(step_up=step_up, first=first, call=call, functions=functions)
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout
    return json.loads(out)


@pytest.mark.skipif(_NODE, reason="node not installed")
def test_a_failed_step_up_shows_confirm_with_google_linking_back_to_the_devices_tab():
    out = _run(step_up="false", first='"passkey_required"')
    assert out["shown"] is True and out["outcome"] == "failed"
    url = urlparse(out["href"])
    assert url.path == "/auth/login/google" and parse_qs(url.query) == {"next": ["/cabinet?tab=devices"]}


@pytest.mark.skipif(_NODE, reason="node not installed")
def test_a_failed_step_up_on_remove_shows_the_fallback_too():
    out = _run(step_up="false", first='"added"', call="removePasskey('k1')")
    assert out["shown"] is True and out["href"].startswith("/auth/login/google?next=")


@pytest.mark.skipif(_NODE, reason="node not installed")
@pytest.mark.parametrize("step_up, first", [("true", '"added"'), ("false", '"added"'), ("false", '"cancelled"')],
                         ids=["step-up-ok", "no-proof-needed", "cancelled-sheet"])
def test_the_fallback_stays_hidden_when_no_proof_was_missing(step_up, first):
    assert _run(step_up=step_up, first=first)["shown"] is False
