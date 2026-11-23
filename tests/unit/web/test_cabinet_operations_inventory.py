"""Every operation the Cabinet offers today, pinned before the redesign moves anything (CABINET_REDESIGN_RFC §6a).

The redesign's main risk is silent loss: a card with three actions becomes a row with two. The lists below are
written out by hand from the page as it stood before step 1, never built from the source at test time (a list read
from the source would follow the source down). Each step may move an operation; none may drop one:

- a rename or a merge edits a list in a `test(mechanical)` commit that maps the old name to the new one;
- a lost operation turns this test red, and the red goes to a reviewer under the test rule. A list never shrinks on
  its own.

HANDLERS: every named function a control of the page calls, from its `on…=` attributes (inline markup and the markup
JS template literals render) and from `addEventListener` (a named listener or a named call inside an inline one).
SERVER_CALLS: every (method, path) the page requests; `{}` stands for a `${…}` part of the path; GET when the call
names no method.
PAGE_TARGETS: the pages the Cabinet sends the browser to (the call pages, a connect flow, the sign-in).
"""
import re

import pytest

from tests.unit.web.cabinet_source import cabinet_source

_HTML = cabinet_source()

HANDLERS = [
    "acceptPasskeyOffer", "addDrive", "addPasskeyHere", "askDisconnectDrive", "backToBrowse", "browseNextPage",
    "cancelDisconnectDrive", "cancelDrivePickerPreview", "cancelGmailIndex", "cancelPinReset", "closeDrivePicker",
    "closeModal", "copyCorrectMessage", "copyDeepLink", "deleteGmailData", "deleteReminder", "disconnectDrive",
    "disconnectGmail", "disconnectService", "dismissPasskeyOffer", "driveCrumb", "driveTabKey", "editReminder",
    "forgotPin", "generateTeamInvite", "handleEsc", "hideReminderForm", "init", "invalidateFactDirect", "linkPlatform",
    "logout", "logoutAllDevices", "logoutDevice", "logoutOtherDevices", "makeDrivePrimary", "navigate",
    "onRecurrenceChange", "onReviewChange", "onReviewClick", "openCorrectModal", "openDrivePicker",
    "openDrivePickerFolder", "openInvalidModal", "relockFacts", "removeDrivePickerChip", "removeDriveScopeFolder",
    "removePasskey", "renameDrive", "requestCloseDrivePicker", "resumeDrivePicker", "runSearch", "saveDailyReview",
    "saveDeepResearch", "saveDeepResearchCopy", "saveDriveConnectorAccess", "saveDriveIndexing", "saveDrivePicker",
    "saveDriveSearchedByDefault", "saveGmailAutoIndex", "saveLanguage", "saveLocation", "saveReminder",
    "saveTimezone", "saveVoiceLanguages", "selectDriveTab", "setDomainFilter", "setDrivePickerFilter",
    "setDrivePickerSource", "setDrivePickerWrite", "setFactsState", "setLangMirror", "setLangUI",
    "showReminderForm", "submitIndexModal", "submitPinChange", "submitPinRemove", "submitPinSet", "toggleDriveCard",
    "toggleDrivePickerFolder", "toggleDrivePickerWhole", "toggleDriveScopeWhole", "triggerGmailIndex",
    "unlinkPlatform", "unlockFacts", "updateIndexModal",
]

SERVER_CALLS = [
    ("GET", "/auth/me"),
    ("POST", "/auth/logout"),
    ("GET", "/api/connectors"),
    ("GET", "/api/user/fact-domains"),
    ("GET", "/api/user/facts/browse"),
    ("POST", "/api/user/facts/search"),
    ("GET", "/api/user/facts/recently-closed"),
    ("POST", "/api/user/facts/{}/invalidate"),
    ("GET", "/api/user/facts/review-queue"),
    ("POST", "/api/user/facts/review-answers"),
    ("GET", "/api/user/facts-pin"),
    ("POST", "/api/user/facts-pin/set"),
    ("POST", "/api/user/facts-pin/change"),
    ("POST", "/api/user/facts-pin/remove"),
    ("POST", "/api/user/facts-pin/reset"),
    ("POST", "/api/user/facts-pin/reset/cancel"),
    ("POST", "/api/user/facts-pin/unlock"),
    ("POST", "/api/user/facts-pin/relock"),
    ("GET", "/api/user/devices"),
    ("POST", "/api/user/devices/{}/logout"),
    ("POST", "/api/user/devices/logout-others"),
    ("POST", "/api/user/devices/logout-all"),
    ("GET", "/api/user/passkeys"),
    ("POST", "/api/user/passkeys/{}/remove"),
    ("GET", "/api/user/timezone"),
    ("PUT", "/api/user/timezone"),
    ("GET", "/api/user/location"),
    ("PUT", "/api/user/location"),
    ("GET", "/api/user/language"),
    ("POST", "/api/user/language"),
    ("GET", "/api/user/voice-languages"),
    ("PUT", "/api/user/voice-languages"),
    ("GET", "/api/user/deep-research"),
    ("PUT", "/api/user/deep-research"),
    ("GET", "/api/user/reminders"),
    ("POST", "/api/user/reminders"),
    ("PUT", "/api/user/reminders/{}"),
    ("DELETE", "/api/user/reminders/{}"),
    ("GET", "/api/user/platforms"),
    ("POST", "/api/user/link-telegram"),
    ("POST", "/api/user/link-platform"),
    ("DELETE", "/api/user/link-platform"),
    ("GET", "/api/user/invite-codes"),
    ("POST", "/api/user/invite-codes"),
    ("GET", "/api/gmail/status"),
    ("GET", "/api/gmail/auto-index"),
    ("PUT", "/api/gmail/auto-index"),
    ("GET", "/api/gmail/daily-review"),
    ("PUT", "/api/gmail/daily-review"),
    ("POST", "/api/gmail/index"),
    ("POST", "/api/gmail/jobs/{}/cancel"),
    ("DELETE", "/api/gmail/data"),
    ("DELETE", "/api/gmail/disconnect"),
    ("GET", "/api/tasks/microsoft/status"),
    ("DELETE", "/api/tasks/microsoft/disconnect"),
    ("GET", "/api/drive/connections"),
    ("DELETE", "/api/drive/connections/{}"),
    ("PUT", "/api/drive/connections/{}/name"),
    ("PUT", "/api/drive/primary"),
    ("PUT", "/api/drive/connections/{}/searched-by-default"),
    ("PUT", "/api/drive/connections/{}/write-folder"),
    ("PUT", "/api/drive/connections/{}/scope"),
    ("POST", "/api/drive/connections/{}/scope/preview"),
    ("PUT", "/api/drive/connections/{}/connector-access"),
    ("GET", "/api/drive/connections/{}/folders"),
    ("PUT", "/api/drive/connections/{}/indexing"),
    ("GET", "/api/drive/deep-research-copy"),
    ("PUT", "/api/drive/deep-research-copy"),
]

PAGE_TARGETS = ["/cabinet/call", "/cabinet/call/modes", "/auth/connect/{}", "/auth/login"]

_VERBS = ("GET", "POST", "PUT", "DELETE", "PATCH")
_TEMPLATE_PART = r"\$\{(?:[^{}]|\{[^{}]*\})*\}"     # a `${…}` part, one level of braces inside allowed
_PATH_END = r"(?![A-Za-z0-9_/\-])"                    # the path ends here: `/x` never matches `/x/y` or `/x-y`
_CALLED = r"(?<![\w.$])([A-Za-z_]\w*)\s*\("           # a bare call `name(`: not a method, not inside `${…}`
_NOT_HANDLERS = {"if", "return", "parseInt", "confirm"}
_WINDOW_LINES = 4                                     # a call's method sits on its path's line or the next three


def _path_pattern(path: str) -> str:
    return "".join(_TEMPLATE_PART if part == "{}" else re.escape(part) for part in re.split(r"(\{\})", path))


def _methods_near(src: str, pattern: str):
    """The HTTP verbs named on the lines of each occurrence of `pattern` (an occurrence naming none is a GET)."""
    lines = src.split("\n")
    for m in re.finditer(pattern + _PATH_END, src):
        first = src.count("\n", 0, m.start())
        window = "\n".join(lines[first:first + _WINDOW_LINES])
        verbs = {v for v in _VERBS if f'"{v}"' in window}
        if "navigator.sendBeacon(" in lines[first]:
            verbs.add("POST")                         # a beacon is always a POST
        yield verbs or {"GET"}


def _call_present(src: str, method: str, path: str) -> bool:
    if any(method in verbs for verbs in _methods_near(src, _path_pattern(path))):
        return True
    # The page's two path helpers: pinRequest(method, sub) under /api/user/facts-pin, postDevices(sub) (a POST)
    # under /api/user/devices/.
    pin = "/api/user/facts-pin"
    if path.startswith(pin) and re.search(rf'pinRequest\("{method}",\s*"{re.escape(path[len(pin):])}"', src):
        return "`${API}/api/user/facts-pin${path}`" in src
    devices = "/api/user/devices/"
    if method == "POST" and path.startswith(devices):
        sub = _path_pattern(path[len(devices):])
        if re.search(r"postDevices\(([`\"])" + sub + r"\1\)", src):
            return "`${API}/api/user/devices/${path}`, { method: \"POST\"" in src
    return False


def _attribute_bodies(src: str):
    for body in re.findall(r'\bon(?:click|change|input|keydown|keyup|submit)="([^"]*)"', src):
        yield re.sub(_TEMPLATE_PART + r"|'[^']*'", "", body)   # `${…}` and quoted arguments are not calls of the control


def _listener_bodies(src: str):
    for m in re.finditer(r"addEventListener\(\s*\"\w+\",\s*\(?\w*\)?\s*=>\s*\{", src):
        i, depth = m.end(), 1
        while depth:
            depth += {"{": 1, "}": -1}.get(src[i], 0)
            i += 1
        yield src[m.end():i]


def _bound_handlers(src: str) -> set:
    names = set()
    for body in [*_attribute_bodies(src), *_listener_bodies(src)]:
        names.update(re.findall(_CALLED, body))
    names.update(re.findall(r"addEventListener\(\s*\"\w+\",\s*([A-Za-z_]\w*)\s*[,)]", src))
    return names - _NOT_HANDLERS


def test_the_lists_hold_no_duplicates():
    assert len(set(HANDLERS)) == len(HANDLERS)
    assert len(set(SERVER_CALLS)) == len(SERVER_CALLS)
    assert len(set(PAGE_TARGETS)) == len(PAGE_TARGETS)


@pytest.mark.parametrize("name", HANDLERS)
def test_every_handler_is_defined_and_bound_to_a_control(name):
    assert re.search(rf"(?:async\s+)?function\s+{name}\s*\(|(?:const|let)\s+{name}\s*=", _HTML), f"{name} is not defined"
    assert name in _bound_handlers(_HTML), f"no control calls {name}"


@pytest.mark.parametrize(("method", "path"), SERVER_CALLS)
def test_every_server_call_is_still_made(method, path):
    assert _call_present(_HTML, method, path), f"{method} {path} is no longer requested"


@pytest.mark.parametrize("target", PAGE_TARGETS)
def test_every_page_target_is_still_reachable(target):
    assert re.search(_path_pattern(target) + _PATH_END, _HTML), target
