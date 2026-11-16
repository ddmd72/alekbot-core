"""The interpreter's "Show" view (VOICE_CALL_LIVE_TEXT_RFC §4.8): a full-screen card with the latest translation large
and its source small above it, opened by the Show button in the call row's right slot or by a tap on a translation
line, following the live text, closed only by Back, turned over by Flip. The static checks read the page; the
behavioural ones run the live-text and Show view blocks under node with a fake DOM (skipped without node)."""
import re

import pytest

from tests.unit.web.call_page_harness import HTML, block, dictionary, needs_node, page_script, run_node, show_view_block

XSS = "<img src=x onerror=alert(1)>"


def _run(body: str, mode: str | None = "interpreter") -> dict:
    return run_node(page_script(mode, "showLiveText();\n" + body))


def _out(**expr: str) -> str:
    return "console.log(JSON.stringify({" + ", ".join(f"{k}: ({v})" for k, v in expr.items()) + "}));"


_TURN = """
send(line("l1", "caller", "set", "Is this Albergo Immaginario?"));
send(line("l2", "lelik", "append", "È questo l'Albergo Immaginario?"));
"""
_VIEW = """
const view = () => document.body.querySelector(".show-view");
const text = (sel) => view().querySelector(sel).textContent;
const showBtn = () => document.getElementById("show-btn");
"""


# ---------------------------------------------------------------- static

def test_the_block_exists_ends_with_its_init_and_uses_no_innerhtml():
    code = show_view_block()
    assert code and code.rstrip().endswith("showViewInit();")
    for banned in ("innerHTML", "insertAdjacentHTML", "outerHTML", "keydown", "Escape"):
        assert banned not in code


@pytest.mark.parametrize("lang", ("en", "uk", "fr", "es"))
def test_every_locale_has_the_show_keys(lang):
    for key in ("show.button", "show.back", "show.flip"):
        assert f'"{key}":' in dictionary(lang)


def test_the_english_labels_are_pinned():
    en = dictionary("en")
    assert '"show.button": "Show"' in en and '"show.back": "Back"' in en and '"show.flip": "Flip"' in en


def test_the_new_css_uses_tokens_only_and_never_reaches_the_overlay_from_a_listener():
    # The Show view's CSS section: from its header to the next section's header (Save as file follows it).
    css = block("      /* ── Show view (VOICE", "      /* ── Save as file (VOICE")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", css)
    assert "-apple-system-body" in css and "rotate(180deg)" in css
    assert "transition" not in css and "animation" not in css
    assert 'role", "dialog"' in show_view_block() and 'aria-modal", "true"' in show_view_block()
    assert HTML.count("new WebSocket(") == 1


# ---------------------------------------------------------------- behaviour under node

@needs_node
@pytest.mark.parametrize("mode, expected", [("interpreter", True), ("tutor", False), (None, False)])
def test_the_control_exists_only_on_the_interpreter_page_inside_the_right_slot(mode, expected):
    out = _run(_VIEW + _out(
        made="!!showBtn()", inSlot="!!document.getElementById('call-slot-right') && "
        "document.getElementById('call-slot-right').querySelector('#show-btn') !== null",
        label="document.body.querySelector('.show-label') && document.body.querySelector('.show-label').textContent",
        svg="showBtn() ? showBtn().children[0].ns : null"), mode=mode)
    assert out["made"] is expected
    if expected:
        assert out["inSlot"] is True
        assert out["label"] == "show.button"
        assert out["svg"] == "http://www.w3.org/2000/svg"


@needs_node
def test_the_control_is_hidden_until_a_translation_exists_and_stays_after_the_call_ends():
    out = _run(_VIEW + """
const ctl = () => document.body.querySelector(".show-ctl");
const start = ctl().classList.contains("off");
send(line("l1", "caller", "set", "Is this Albergo Immaginario?"));
const callerOnly = ctl().classList.contains("off");
send(line("l2", "lelik", "append", "È questo l'Albergo Immaginario?", { kind: "ask" }));
const askOnly = ctl().classList.contains("off");
send(line("l3", "lelik", "append", "È questo l'Albergo Immaginario?"));
const live = ctl().classList.contains("off");
endLiveText();
const ended = ctl().classList.contains("off");
resetLiveText();
const reset = ctl().classList.contains("off");
""" + _out(start="start", callerOnly="callerOnly", askOnly="askOnly", live="live", ended="ended", reset="reset"))
    assert out == {"start": True, "callerOnly": True, "askOnly": True, "live": False, "ended": False, "reset": True}


@needs_node
def test_the_show_button_opens_the_latest_translation_with_its_source_above():
    out = _run(_VIEW + _TURN + """
const before = !!view();
showBtn().click();
""" + _out(before="before", hidden="view().hidden", role="view().getAttribute('role')",
           modal="view().getAttribute('aria-modal')", source="text('.show-source')",
           translation="text('.show-translation')", focus="document.activeElement.className",
           order="view().children[0].children.map((c) => c.className)"))
    assert out["before"] is False
    assert out["hidden"] is False and out["role"] == "dialog" and out["modal"] == "true"
    assert out["source"] == "Is this Albergo Immaginario?"
    assert out["translation"] == "È questo l'Albergo Immaginario?"
    assert out["focus"] == "show-back"
    assert out["order"] == ["show-source", "show-translation"]


@needs_node
def test_a_tap_on_an_older_translation_opens_the_view_on_the_latest_one_and_a_block_line_does_not_open_it():
    out = _run(_VIEW + """
send(line("l1", "caller", "set", "Is there a train to Coimbra today?"));
send(line("l2", "lelik", "append", "Há um comboio para Coimbra hoje?"));
send(line("l3", "caller", "set", "Is this Albergo Immaginario?"));
send(line("l4", "lelik", "append", "È questo l'Albergo Immaginario?"));
send(line("l5", "caller", "set", "Lelik, how do I say thank you in Italian?", { kind: "ask" }));
send(line("l6", "lelik", "append", "Grazie.", { kind: "ask" }));
const lelikLines = lineEls().filter((e) => e.matches(".lelik"));
lelikLines[lelikLines.length - 1].click();   // the answer inside the Lelik block
const viaBlock = !!view();
lelikLines[0].click();                        // an older translation
""" + _out(viaBlock="viaBlock", translation="text('.show-translation')", source="text('.show-source')"))
    assert out == {"viaBlock": False, "translation": "È questo l'Albergo Immaginario?", "source": "Is this Albergo Immaginario?"}


@needs_node
def test_the_view_follows_the_live_text_and_a_late_caller_line_fills_in_the_source():
    out = _run(_VIEW + """
send(line("l2", "lelik", "append", "È questo l'Albergo"));
showBtn().click();
const noSource = { source: text('.show-source'), hidden: view().querySelector('.show-source').hidden };
send(line("l2", "lelik", "append", " Immaginario?"));
const grown = text('.show-translation');
send(line("l1", "caller", "set", "Is this Albergo Immaginario?"));
const filled = { source: text('.show-source'), hidden: view().querySelector('.show-source').hidden };
send(line("l3", "caller", "set", "Is there a train to Coimbra today?"));
send(line("l4", "lelik", "append", "Há um comboio para Coimbra hoje?"));
""" + _out(noSource="noSource", grown="grown", filled="filled", latest="text('.show-translation')",
           latestSource="text('.show-source')"))
    assert out["noSource"] == {"source": "", "hidden": True}
    assert out["grown"] == "È questo l'Albergo Immaginario?"
    assert out["filled"] == {"source": "Is this Albergo Immaginario?", "hidden": False}
    assert out["latest"] == "Há um comboio para Coimbra hoje?"
    assert out["latestSource"] == "Is there a train to Coimbra today?"


@needs_node
def test_a_source_from_before_the_previous_translation_is_not_shown():
    out = _run(_VIEW + """
send(line("l1", "caller", "set", "Is this Albergo Immaginario?"));
send(line("l2", "lelik", "append", "È questo l'Albergo Immaginario?"));
send(line("l3", "lelik", "append", "Há um comboio para Coimbra hoje?"));
showBtn().click();
""" + _out(source="text('.show-source')", translation="text('.show-translation')"))
    assert out == {"source": "", "translation": "Há um comboio para Coimbra hoje?"}


@needs_node
def test_an_ask_block_arriving_leaves_the_shown_translation_unchanged():
    out = _run(_VIEW + _TURN + """
showBtn().click();
send(line("l3", "caller", "set", "Lelik, how do I say thank you in Italian?", { kind: "ask" }));
send(line("l4", "lelik", "append", "Grazie.", { kind: "ask" }));
""" + _out(translation="text('.show-translation')", source="text('.show-source')"))
    assert out == {"translation": "È questo l'Albergo Immaginario?", "source": "Is this Albergo Immaginario?"}


@needs_node
def test_only_back_closes_the_view_and_flip_turns_the_card_alone():
    out = _run(_VIEW + _TURN + """
showBtn().click();
const card = view().querySelector(".show-card");
const flip = view().querySelector(".show-flip"); const back = view().querySelector(".show-back");
card.click(); view().click(); view().querySelector(".show-source").click();
const afterTaps = view().hidden;
flip.click();
const flipped = { card: card.classList.contains("flipped"), view: view().classList.contains("flipped"),
                  back: back.classList.contains("flipped"), bar: view().children[1].classList.contains("flipped") };
flip.click(); const unflipped = card.classList.contains("flipped");
flip.click(); back.click();
const closed = { hidden: view().hidden, flipped: card.classList.contains("flipped") };
showBtn().click();
const reopened = { hidden: view().hidden, flipped: card.classList.contains("flipped") };
""" + _out(afterTaps="afterTaps", flipped="flipped", unflipped="unflipped", closed="closed", reopened="reopened",
           listeners="[view().listeners.click, card.listeners.click].every((l) => l === undefined)"))
    assert out["afterTaps"] is False
    assert out["flipped"] == {"card": True, "view": False, "back": False, "bar": False}
    assert out["unflipped"] is False
    assert out["closed"] == {"hidden": True, "flipped": False}
    assert out["reopened"] == {"hidden": False, "flipped": False}
    assert out["listeners"] is True


@needs_node
def test_an_interrupted_translation_ends_with_an_ellipsis():
    out = _run(_VIEW + _TURN + """
showBtn().click();
send(endLine("l2", "interrupted"));
""" + _out(translation="text('.show-translation')", stored="liveText.lines[1].text"))
    assert out == {"translation": "È questo l'Albergo Immaginario?…", "stored": "È questo l'Albergo Immaginario?"}


@needs_node
def test_a_new_call_closes_the_view_and_resets_the_flip():
    out = _run(_VIEW + _TURN + """
showBtn().click();
view().querySelector(".show-flip").click();
resetLiveText();
""" + _out(hidden="view().hidden", flipped="view().querySelector('.show-card').classList.contains('flipped')",
           off="document.body.querySelector('.show-ctl').classList.contains('off')"))
    assert out == {"hidden": True, "flipped": False, "off": True}


@needs_node
def test_text_in_the_view_is_set_as_text():
    out = _run(_VIEW + f"""
send(line("l1", "caller", "set", {XSS!r}));
send(line("l2", "lelik", "append", {XSS!r}));
showBtn().click();
""" + _out(source="text('.show-source')", translation="text('.show-translation')",
           imgs="document.body.querySelectorAll('img').length"))
    assert out == {"source": XSS, "translation": XSS, "imgs": 0}
