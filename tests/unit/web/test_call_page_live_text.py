"""The live text panel of the mode call pages (VOICE_CALL_LIVE_TEXT_RFC §4.4, §4.7-§4.9): the relay's `line` and
`snapshot` frames on the control channel become a scrolling panel on the interpreter and tutor pages, and nothing on
`/cabinet/call`. The static checks read the page; the behavioural ones run the page's live-text block (and the Show
view block when the page has one) under node with a fake DOM (skipped when node is not installed)."""
import re

import pytest

from tests.unit.web.call_page_harness import HTML, block, dictionary, live_text_block, needs_node, page_script, run_node

XSS = "<img src=x onerror=alert(1)>"
LANGS = ("en", "uk", "fr", "es")
TEXT_KEYS = ("text.panel", "text.you", "text.lelik", "text.latest", "text.heard_hint")


def _run(body: str, mode: str | None = "interpreter") -> dict:
    return run_node(page_script(mode, "showLiveText();\n" + body))


def _out(**expr: str) -> str:
    """A node statement printing the given JS expressions as one JSON line."""
    return "console.log(JSON.stringify({" + ", ".join(f"{k}: ({v})" for k, v in expr.items()) + "}));"


# ---------------------------------------------------------------- static

def test_the_page_markup_gains_no_text_or_show_element():
    body = HTML[HTML.index("<body>"):HTML.index("<script>", HTML.index("<body>"))]
    for token in ("text-", "show", "panel", "live-text"):
        assert token not in body.lower()


@pytest.mark.parametrize("name", ["showLiveText", "resetLiveText", "endLiveText", "onLiveTextFrame"])
def test_every_entry_function_returns_at_the_mode_gate(name):
    body = block(f"      function {name}(", "\n      }\n")
    first = body.splitlines()[1].strip()
    assert first == "if (!LIVE_TEXT_MODES.includes(window.VOICE_MODE)) return;"
    assert 'const LIVE_TEXT_MODES = ["interpreter", "tutor"];' in live_text_block()


def test_the_four_hooks_sit_where_the_card_says():
    start_call = re.search(r"async function startCall\(\) \{.*?\n      }\n", HTML, re.S).group(0)
    assert "inCall = true;\n        resetLiveText();\n" in start_call
    assert "if (connectBody.control) showAsk();\n          if (connectBody.control) showLiveText();\n" in HTML
    end_call = re.search(r"async function endCall\(.*?\n      }\n", HTML, re.S).group(0)
    # Every hang-up ends the live text: at once here unless a user hang-up lingers, and then through
    # finishLiveTextLinger (bye, socket gone, cap: the behavioural tests below).
    assert re.search(r"hideSpeaker\(\);\n(?:        //[^\n]*\n)*        if \(!lingerLiveText\(trigger\)\) \{\n"
                     r"          closeControl\(\);\n          endLiveText\(\);\n        \}\n", end_call)
    pagehide = HTML[HTML.index('addEventListener("pagehide"'):]
    pagehide = pagehide[:pagehide.index("});")]
    assert "stopRingback();\n        resetLiveText();" in pagehide
    assert "fetch(" not in pagehide
    assert HTML.count('addEventListener("pagehide"') == 1


def test_the_block_has_no_unsafe_dom_or_storage_api():
    code = live_text_block()
    for banned in ("innerHTML", "insertAdjacentHTML", "outerHTML", "localStorage", "sessionStorage"):
        assert banned not in code


@pytest.mark.parametrize("lang", LANGS)
def test_every_locale_has_the_five_text_keys(lang):
    for key in TEXT_KEYS:
        assert f'"{key}":' in dictionary(lang)


def test_the_english_labels_are_pinned():
    en = dictionary("en")
    assert '"text.panel": "Call text"' in en
    assert '"text.you": "You"' in en
    assert '"text.lelik": "Lelik"' in en
    assert '"text.latest": "Latest ↓"' in en
    assert '"text.heard_hint": "Your words as the recogniser heard them"' in en


def test_the_new_css_uses_tokens_only_and_scales_with_dynamic_type():
    css = block("      /* ── Live text panel", "      .text-pill[hidden] {\n        display: none;\n      }")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", css)
    assert "-apple-system-body" in css
    assert "transition" not in css and "animation" not in css


# ---------------------------------------------------------------- behaviour under node

@needs_node
def test_the_main_page_creates_nothing_and_keeps_no_lines():
    out = _run("""
send(line("l1", "lelik", "append", "È questo l'Albergo Immaginario?")); send(snapshot([{ line: "l2", who: "caller", text: "x" }]));
""" + _out(lines="liveText.lines.length", panel="!!panelOf()", cls="[...SCREEN._cls]", live="liveText.live",
           last="liveText.lastSeq"), mode=None)
    assert out == {"lines": 0, "panel": False, "cls": ["screen"], "live": False, "last": -1}


@needs_node
def test_append_concatenates_increments_and_end_sets_the_status_without_touching_the_text():
    out = _run("""
send(line("l1", "lelik", "append", "È questo l'Albergo"));
send(line("l1", "lelik", "append", " Immaginario?"));
send(endLine("l1", "done"));
const done = lineEls()[0].textContent;
send(line("l2", "lelik", "append", "Há um comboio"));
send(endLine("l2", "interrupted"));
""" + _out(done="done", shown="lineEls()[1].textContent", stored="liveText.lines.map((l) => [l.text, l.status])"))
    assert out["done"] == "È questo l'Albergo Immaginario?"
    assert out["shown"] == "Há um comboio…"
    assert out["stored"] == [["È questo l'Albergo Immaginario?", "done"], ["Há um comboio", "interrupted"]]


@needs_node
def test_a_set_replaces_the_callers_text_and_the_lines_carry_their_labels_and_classes():
    out = _run("""
send(line("l1", "caller", "set", "Is this Albergo"));
send(line("l1", "caller", "set", "Is this Albergo Immaginario?"));
send(line("l2", "lelik", "append", "È questo l'Albergo Immaginario?"));
const [caller, lelik] = lineEls();
""" + _out(callerText="caller.textContent", callerCls="caller.className", label="caller.children[0].textContent",
           lelikCls="lelik.className", lelikKids="lelik.children.length", n="lineEls().length"))
    assert out["callerText"] == "text.youIs this Albergo Immaginario?"
    assert out["callerCls"] == "text-line caller"
    assert out["label"] == "text.you"
    assert out["lelikCls"] == "text-line lelik"
    assert out["lelikKids"] == 1
    assert out["n"] == 2


@needs_node
def test_a_repeated_or_older_seq_and_a_frame_without_a_seq_or_of_another_version_are_dropped():
    out = _run("""
const first = line("l1", "lelik", "append", "Tem ");
send(first); send(first);
const afterRepeat = lineEls()[0].textContent;
send(Object.assign(line("l1", "lelik", "append", "X"), { seq: 0 }));
send(Object.assign(line("l1", "lelik", "append", "X"), { seq: undefined }));
send(Object.assign(line("l1", "lelik", "append", "X"), { seq: "9" }));
send(Object.assign(line("l1", "lelik", "append", "X"), { seq: 50, v: 2 }));
send(Object.assign(line("l1", "lelik", "append", "X"), { seq: 51, type: "other" }));
send(null);
""" + _out(afterRepeat="afterRepeat", text="lineEls()[0].textContent", last="liveText.lastSeq"))
    assert out == {"afterRepeat": "Tem ", "text": "Tem ", "last": 1}


@needs_node
def test_an_ask_state_seq_does_not_block_a_later_line():
    out = _run("""
send({ type: "ask_state", v: 1, seq: 99, state: "idle" });
send(Object.assign(line("l1", "lelik", "append", "È questo l'Albergo Immaginario?"), { seq: 5 }));
""" + _out(text="lineEls()[0].textContent", last="liveText.lastSeq"))
    assert out == {"text": "È questo l'Albergo Immaginario?", "last": 5}


@needs_node
def test_a_status_frame_advances_the_seq_and_changes_nothing_else():
    out = _run("""
send({ v: 1, seq: 7, type: "status", state: "thinking" });
send(Object.assign(line("l1", "lelik", "append", "Tem"), { seq: 7 }));
""" + _out(n="liveText.lines.length", last="liveText.lastSeq"))
    assert out == {"n": 0, "last": 7}


@needs_node
def test_a_callers_line_whose_text_arrives_late_renders_above_the_reply_that_started_first():
    out = _run("""
send(line("l8", "lelik", "append", "Há um comboio para Coimbra hoje?"));
send(line("l7", "caller", "set", "Is there a train to Coimbra today?"));
""" + _out(order="liveText.lines.map((l) => l.line)", who="lineEls().map((e) => e.className)"))
    assert out == {"order": ["l7", "l8"], "who": ["text-line caller", "text-line lelik"]}


@needs_node
def test_a_snapshot_replaces_the_view_merges_by_id_and_drops_an_old_seq():
    out = _run("""
send(line("l1", "caller", "set", "Is there a train to Coimbra today?"));
send(line("l2", "lelik", "append", "Há um comboio"));
send(snapshot([{ line: "l2", who: "lelik", text: "Há um comboio para Coimbra hoje?", status: "done" },
               { line: "l3", who: "caller", text: "Is this Albergo Immaginario?" }]));
const merged = { lines: liveText.lines.map((l) => [l.line, l.text, l.status]), shown: lineEls().map((e) => e.textContent) };
const stale = snapshot([{ line: "l9", who: "lelik", text: "stale" }]);
stale.seq = 1;
send(stale);
send(null);
send(snapshot([{ line: "l2", who: "lelik", text: "È questo l'Albergo Immaginario?", status: "done" }]));
""" + _out(merged="merged", after="liveText.lines.map((l) => [l.line, l.text])", shown="lineEls().map((e) => e.textContent)"))
    assert out["merged"]["lines"] == [
        ["l1", "Is there a train to Coimbra today?", "open"],
        ["l2", "Há um comboio para Coimbra hoje?", "done"],
        ["l3", "Is this Albergo Immaginario?", "open"],
    ]
    assert len(out["merged"]["shown"]) == 3
    assert out["after"] == [["l1", "Is there a train to Coimbra today?"], ["l2", "È questo l'Albergo Immaginario?"],
                            ["l3", "Is this Albergo Immaginario?"]]
    assert out["shown"] == ["text.youIs there a train to Coimbra today?", "È questo l'Albergo Immaginario?",
                            "text.youIs this Albergo Immaginario?"]


@needs_node
def test_the_socket_dropping_leaves_the_view_and_the_next_snapshot_restores_it():
    out = _run("""
send(line("l1", "lelik", "append", "È questo l'Albergo Immaginario?"));
send(null);
const kept = lineEls().length;
send(snapshot([{ line: "l1", who: "lelik", text: "È questo l'Albergo Immaginario?", status: "done" },
               { line: "l2", who: "caller", text: "Is this Albergo Immaginario?" }]));
""" + _out(kept="kept", n="lineEls().length"))
    assert out == {"kept": 1, "n": 2}


@needs_node
def test_ask_lines_make_one_labelled_block_and_other_lines_stay_outside_it():
    out = _run("""
send(line("l1", "caller", "set", "Is there a train to Coimbra today?"));
send(line("l2", "lelik", "append", "Há um comboio para Coimbra hoje?"));
send(line("l3", "caller", "set", "Lelik, how do I say thank you in Italian?", { kind: "ask" }));
send(line("l4", "lelik", "append", "Grazie.", { kind: "ask" }));
send(line("l5", "lelik", "append", "È questo l'Albergo Immaginario?"));
send(line("l6", "lelik", "append", "Alone.", { kind: "ask" }));
const panel = panelOf();
const blocks = panel.querySelectorAll(".text-block");
""" + _out(blocks="blocks.length", label="blocks[0].children[0].textContent", inside="blocks[0].children.length",
           texts="blocks[0].querySelectorAll('.text-line').map((e) => e.textContent)",
           direct="panel.children.filter((c) => c.matches('.text-line')).length",
           lonely="blocks[1].children.map((c) => c.textContent)"))
    assert out["blocks"] == 2
    assert out["label"] == "text.lelik"
    assert out["inside"] == 3
    assert out["texts"] == ["Lelik, how do I say thank you in Italian?", "Grazie."]
    assert out["direct"] == 3
    assert out["lonely"] == ["text.lelik", "Alone."]


@needs_node
def test_the_dom_holds_fifty_lines_and_the_list_two_thousand():
    out = _run("""
for (let i = 1; i <= 60; i++) send(line("l" + i, "lelik", "append", "n" + i));
const first = { dom: lineEls().length, list: liveText.lines.length, firstShown: lineEls()[0].textContent };
for (let i = 61; i <= 2005; i++) send(line("l" + i, "lelik", "append", "n" + i));
""" + _out(first="first", dom="lineEls().length", list="liveText.lines.length", oldest="liveText.lines[0].line",
           newest="liveText.lines[liveText.lines.length - 1].line", hint="panelOf().children.length"))
    assert out["first"] == {"dom": 50, "list": 60, "firstShown": "n11"}
    assert out["dom"] == 50
    assert out["list"] == 2000
    assert out["oldest"] == "l6"
    assert out["newest"] == "l2005"
    assert out["hint"] == 51  # the one-time hint is not counted in the cap


@needs_node
def test_frame_text_is_set_as_text_and_creates_no_element():
    out = _run(f"""
send(line("l1", "caller", "set", {XSS!r}));
send(line("l2", "lelik", "append", {XSS!r}));
""" + _out(texts="lineEls().map((e) => e.children[e.children.length - 1].textContent)",
           imgs="document.body.querySelectorAll('img').length"))
    assert out == {"texts": [XSS, XSS], "imgs": 0}


@needs_node
def test_scroll_follows_at_the_bottom_and_holds_with_a_pill_when_scrolled_up():
    out = _run("""
send(line("l1", "lelik", "append", "È questo l'Albergo Immaginario?"));
const panel = panelOf(); const pill = document.body.querySelector(".text-pill");
panel.scrollHeight = 1000; panel.clientHeight = 500; panel.scrollTop = 500;
send(line("l2", "lelik", "append", "Há um comboio"));
const atBottom = { top: panel.scrollTop, pill: pill.hidden };
panel.scrollHeight = 1400; panel.scrollTop = 100;
send(line("l3", "lelik", "append", "È questo l'Albergo Immaginario?"));
const held = { top: panel.scrollTop, pill: pill.hidden };
pill.click();
const tapped = { top: panel.scrollTop, pill: pill.hidden };
panel.scrollTop = 100; send(line("l4", "lelik", "append", "x")); const shownAgain = pill.hidden;
panel.scrollTop = 900; panel.fire("scroll");
""" + _out(atBottom="atBottom", held="held", tapped="tapped", shownAgain="shownAgain", afterScroll="pill.hidden"))
    assert out["atBottom"] == {"top": 1000, "pill": True}
    assert out["held"] == {"top": 100, "pill": False}
    assert out["tapped"] == {"top": 1400, "pill": True}
    assert out["shownAgain"] is False
    assert out["afterScroll"] is True


@needs_node
def test_the_layout_class_arrives_with_the_first_line_and_the_panel_is_a_silent_log():
    out = _run("""
const before = { cls: [...SCREEN._cls], panel: !!panelOf() };
send(line("l1", "lelik", "append", "È questo l'Albergo Immaginario?"));
const p = panelOf();
""" + _out(before="before", cls="[...SCREEN._cls]", role="p.getAttribute('role')", live="p.getAttribute('aria-live')",
           name="p.getAttribute('aria-label')", hint="p.children[0].textContent",
           order="SCREEN.children.map((c) => c.className)"))
    assert out["before"] == {"cls": ["screen"], "panel": False}
    assert out["cls"] == ["screen", "text-live"]
    assert out["role"] == "log" and out["live"] == "off" and out["name"] == "text.panel"
    assert out["hint"] == "text.heard_hint"
    assert out["order"] == ["identity", "text-wrap", "controls"]


@needs_node
def test_end_keeps_the_text_and_ignores_later_frames_and_reset_clears_everything_for_the_next_call():
    out = _run("""
send(line("l1", "lelik", "append", "È questo l'Albergo Immaginario?"));
endLiveText();
send(line("l2", "lelik", "append", "late"));
const ended = { n: lineEls().length, cls: SCREEN.classList.contains("text-live"), lines: liveText.lines.length };
resetLiveText();
const reset = { n: lineEls().length, panel: !!panelOf(), cls: SCREEN.classList.contains("text-live"),
                lines: liveText.lines.length, last: liveText.lastSeq, live: liveText.live };
showLiveText();
send({ v: 1, seq: 1, type: "line", line: "l1", who: "caller", op: "set", text: "Is this Albergo Immaginario?" });
""" + _out(ended="ended", reset="reset", again="lineEls().length"))
    assert out["ended"] == {"n": 1, "cls": True, "lines": 1}
    assert out["reset"] == {"n": 0, "panel": False, "cls": False, "lines": 0, "last": -1, "live": False}
    assert out["again"] == 1


@needs_node
def test_ending_a_call_that_never_showed_a_line_leaves_the_page_as_it_was():
    out = _run("endLiveText();\n" + _out(cls="[...SCREEN._cls]", panel="!!panelOf()"))
    assert out == {"cls": ["screen"], "panel": False}


@needs_node
def test_an_empty_set_removes_the_line():
    out = _run("""
send(line("l1", "caller", "set", "Is this Albergo Immaginario?"));
send(line("l2", "lelik", "append", "È questo l'Albergo Immaginario?"));
send(line("l1", "caller", "set", ""));
send(line("l9", "caller", "set", ""));
""" + _out(order="liveText.lines.map((l) => l.line)", n="lineEls().length"))
    assert out == {"order": ["l2"], "n": 1}


@needs_node
def test_the_tutor_page_gets_the_panel_too():
    out = _run("send(line(\"l1\", \"lelik\", \"append\", \"È questo l'Albergo Immaginario?\"));\n"
               + _out(n="lineEls().length", cls="[...SCREEN._cls]"), mode="tutor")
    assert out == {"n": 1, "cls": ["screen", "text-live"]}


@needs_node
def test_a_question_split_by_a_pause_stays_in_one_block_with_its_answer():
    out = _run("""
send(line("l1", "caller", "set", "Lelik, how do I say", { kind: "ask" }));
send(line("l2", "caller", "set", "thank you in Italian?", { kind: "ask" }));
send(line("l3", "lelik", "append", "Grazie.", { kind: "ask" }));
send(line("l4", "caller", "set", "Lelik, how do I say", { kind: "ask" }));
send(line("l5", "lelik", "append", "Grazie.", { kind: "ask" }));
const blocks = panelOf().querySelectorAll(".text-block");
""" + _out(n="blocks.length", first="blocks[0].querySelectorAll('.text-line').map((e) => e.textContent)",
           second="blocks[1].querySelectorAll('.text-line').length"))
    assert out == {"n": 2, "first": ["Lelik, how do I say", "thank you in Italian?", "Grazie."], "second": 2}


_HANGUP = """
const closes = []; const timers = [];
globalThis.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
globalThis.clearTimeout = () => {};
let control = { ws: {}, done: false };
function closeControl() { closes.push(1); control = null; send(null); }
send(line("l1", "lelik", "append", "È questo l'Albergo"));
"""


@needs_node
def test_after_a_user_hangup_frames_are_applied_until_bye_and_then_live_text_ends():
    out = _run(_HANGUP + """
const lingering = lingerLiveText("button");
send(line("l1", "lelik", "append", " Immaginario?"));
send(endLine("l1", "interrupted"));
const before = { text: lineEls()[0].textContent, closes: closes.length, live: liveText.live };
send({ type: "bye", v: 1, reason: "call_ended" });
send(line("l1", "lelik", "append", " late"));
""" + _out(lingering="lingering", before="before", ms="timers[0].ms", closes="closes.length", live="liveText.live",
           text="lineEls()[0].textContent"))
    assert out["lingering"] is True and out["ms"] == 3000
    assert out["before"] == {"text": "È questo l'Albergo Immaginario?…", "closes": 0, "live": True}
    assert out["closes"] == 1 and out["live"] is False
    assert out["text"] == "È questo l'Albergo Immaginario?…"


@needs_node
def test_after_a_user_hangup_the_socket_going_away_ends_the_wait_and_the_live_text():
    out = _run(_HANGUP + """
lingerLiveText("button");
send(line("l1", "lelik", "append", " Immaginario?"));
send(null);
send(line("l1", "lelik", "append", " late"));
""" + _out(closes="closes.length", live="liveText.live", text="lineEls()[0].textContent"))
    assert out == {"closes": 1, "live": False, "text": "È questo l'Albergo Immaginario?"}


@needs_node
def test_the_cap_ends_the_wait_and_a_frame_after_it_is_ignored():
    out = _run(_HANGUP + """
lingerLiveText("button");
timers[0].fn();
send(line("l1", "lelik", "append", " late"));
""" + _out(closes="closes.length", live="liveText.live", text="lineEls()[0].textContent"))
    assert out == {"closes": 1, "live": False, "text": "È questo l'Albergo"}


@needs_node
def test_no_wait_when_the_relay_ended_the_call_or_there_is_no_socket_and_a_new_call_cancels_it():
    out = _run(_HANGUP + """
const status = lingerLiveText("status_ended");
const startFailed = lingerLiveText("start_failed");
control = null; const noSocket = lingerLiveText("button");
control = { ws: {}, done: false };
lingerLiveText("button"); resetLiveText(); finishLiveTextLinger();
""" + _out(status="status", startFailed="startFailed", noSocket="noSocket", closes="closes.length"))
    assert out == {"status": False, "startFailed": False, "noSocket": False, "closes": 0}


@needs_node
def test_the_main_page_never_waits():
    out = _run(_HANGUP + _out(w='lingerLiveText("button")'), mode=None)
    assert out == {"w": False}
