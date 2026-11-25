"""The Cabinet's Memory section (CABINET_REDESIGN_RFC §5.2), its page logic run under node: the life-area select with
counts (only non-empty areas, sorted by count, the total on the button, hidden for "closed"; choosing an area filters
the browse list), the facts as rows that open to Edit and Delete (the same modal handlers as the old cards), closed
facts as read-only rows, the review questions collapsed into one row, and the section's new strings in four languages.

The script is `cabinet-strings.js` + a small DOM stand-in + `cabinet-memory.js`, written to a file and run by node (one
command-line argument is limited to ~128 KB on Linux). Facts and areas are the invented universe's: Alex's morning yoga,
the car insurance with Example Mutual Insurance (Transport and Money), tennis on Sundays (no profile yet)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.web.cabinet_source import STATIC

_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
_MEMORY_JS = (STATIC / "cabinet-memory.js").read_text(encoding="utf-8")
_STRINGS_JS = (STATIC / "cabinet-strings.js").read_text(encoding="utf-8")
LANGS = ("en", "uk", "fr", "es")

# A DOM stand-in: elements by id, created on first use, with the fields and methods the section's code touches.
_PRELUDE = r"""
const _els = {};
const els = new Proxy({}, { get: (_, id) => el(id) });
function el(id) {
  if (_els[id]) return _els[id];
  const classes = new Set(), attrs = {};
  const e = _els[id] = {
    id, value: "", textContent: "", innerHTML: "", placeholder: "", disabled: false, dataset: {}, style: {}, attrs,
    classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c),
      toggle: (c, on) => { (on === undefined ? !classes.has(c) : on) ? classes.add(c) : classes.delete(c); } },
    has: (c) => classes.has(c),
    setAttribute: (k, v) => { attrs[k] = String(v); }, getAttribute: (k) => attrs[k],
    insertAdjacentHTML: (where, html) => { e.innerHTML += html; },
    querySelector: () => null, querySelectorAll: () => [], addEventListener: () => {}, focus: () => {},
  };
  return e;
}
const listeners = [];
globalThis.document = { getElementById: el, addEventListener: (type, fn) => listeners.push([type, fn]),
                        querySelector: () => null, querySelectorAll: () => [] };
globalThis.window = { addEventListener: () => {}, matchMedia: () => ({ matches: false }) };
globalThis.CSS = { escape: (s) => s };
const clipboard = [];
Object.defineProperty(globalThis, "navigator", { configurable: true, value: {   // node 21+ has a read-only one
  clipboard: { writeText: async (text) => { clipboard.push(text); } }, sendBeacon: () => true } });
let _currentLang = "en", _activeSection = "memory", allFacts = [], factsPin = { has_pin: false, unlocked: false };
const API = "";
function t(key) { return (LANG_DICT[_currentLang] || LANG_DICT["en"])[key] || key; }
function escapeHtml(str) { return String(str ?? "").replace(/[&<>"']/g, (c) => "&#" + c.charCodeAt(0) + ";"); }
const toasts = [], modals = [];
function showToast(msg, type) { toasts.push([msg, type]); }
function observeElements() {}
function renderModal(id, title, body, footer) { modals.push({ id, title, body, footer }); }
function closeModal() { modals.push("closed"); }
// The server: each request is recorded; `routes` answers by path prefix.
const requests = [];
let routes = {};
async function authFetch(url, init) {
  requests.push({ url, method: (init && init.method) || "GET" });
  const path = Object.keys(routes).find((p) => url.startsWith(p));
  const body = path ? routes[path] : {};
  return { ok: true, status: 200, json: async () => body, clone() { return this; } };
}
const settle = () => new Promise((r) => setTimeout(r, 0));
"""

DOMAINS = [
    {"key": "sport", "label": "Sport", "order": 1},
    {"key": "transport", "label": "Transport", "order": 2},
    {"key": "money", "label": "Money", "order": 3},
    {"key": "food", "label": "Food and drinks", "order": 4},
    {"key": "pets", "label": "Pets", "order": 5},
]
FACTS = [
    {"id": "f-yoga", "text": "morning yoga 30 min", "primary_domain": "sport", "created_at": "2026-10-07T08:00:00Z"},
    {"id": "f-insurance", "text": "Alex's car insurance with Example Mutual Insurance", "primary_domain": "transport",
     "created_at": "2026-10-01T08:00:00Z"},
    {"id": "f-tennis", "text": "plays tennis on Sundays", "primary_domain": None, "created_at": "2026-09-20T08:00:00Z"},
]
COUNTS = {"total": 3, "counts": {"transport": 1, "sport": 2, "money": 1}}


def _run(tmp_path: Path, body: str):
    script = "\n".join([_STRINGS_JS, _PRELUDE, _MEMORY_JS,
                        f"const DOMAINS = {json.dumps(DOMAINS)}, FACTS = {json.dumps(FACTS)};",
                        f"const COUNTS = {json.dumps(COUNTS)};",
                        "function useDomains() { factDomains = DOMAINS; "
                        "factDomainLabels = Object.fromEntries(DOMAINS.map((d) => [d.key, d.label])); }",
                        "(async () => {\n" + body + "\n})().then((out) => process.stdout.write(JSON.stringify(out)));"])
    path = tmp_path / "memory_ui.js"
    path.write_text(script, encoding="utf-8")
    out = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _options(html: str):
    """(data-domain, label, count) of each option of the area list, in order."""
    return re.findall(r'data-domain="([^"]+)"[^>]*><span>([^<]*)</span><span class="c-pick-count">([^<]*)</span>', html)


# ------------------------------------------------------------------------------------------------ the area select

@_node
def test_the_area_list_shows_only_areas_with_facts_fullest_first_and_says_how_many_are_hidden(tmp_path):
    out = _run(tmp_path, """useDomains(); factsMode = "browse"; areaCounts = { state: "current", ...COUNTS };
      renderAreaSelect(); openAreaList();
      return { list: els["area-list"].innerHTML, label: els["area-select-label"].textContent,
        count: els["area-select-count"].textContent, foot: els["area-foot"].textContent,
        footHidden: els["area-foot"].has("hidden"), note: els["area-note"].textContent,
        open: !els["area-pop"].has("hidden"), hidden: els["area-select"].has("hidden") };""")
    assert _options(out["list"]) == [("all", "All areas", "3"), ("sport", "Sport", "2"),
                                     ("transport", "Transport", "1"), ("money", "Money", "1")]
    assert (out["label"], out["count"]) == ("All areas", "· 3")
    assert (out["foot"], out["footHidden"]) == ("Empty areas hidden: 2", False)
    assert "do not add up to the total" in out["note"]
    assert out["open"] and not out["hidden"]


@_node
def test_the_filter_field_narrows_the_list_and_keeps_all_areas(tmp_path):
    out = _run(tmp_path, """useDomains(); factsMode = "browse"; areaCounts = { state: "current", ...COUNTS };
      openAreaList(); els["area-filter"].value = "MON"; filterAreas(); return els["area-list"].innerHTML;""")
    assert [o[0] for o in _options(out)] == ["all", "money"]


@_node
def test_without_counts_the_list_offers_every_area_in_order_and_the_button_shows_no_number(tmp_path):
    out = _run(tmp_path, """useDomains(); factsMode = "browse"; areaCounts = null;
      renderAreaSelect(); openAreaList();
      return [els["area-list"].innerHTML, els["area-select-count"].textContent, els["area-foot"].has("hidden")];""")
    assert [o[0] for o in _options(out[0])] == ["all", "sport", "transport", "money", "food", "pets"]
    assert out[1:] == ["", True]


@_node
def test_the_select_is_hidden_for_closed_and_over_search_results(tmp_path):
    out = _run(tmp_path, """useDomains(); factsMode = "browse"; areaCounts = { state: "current", ...COUNTS };
      openAreaList(); factsState = "closed"; renderAreaSelect();
      const closed = [els["area-select"].has("hidden"), els["area-pop"].has("hidden"), areaListOpen];
      factsState = "current"; factsMode = "search"; renderAreaSelect();
      const search = els["area-select"].has("hidden");
      factsMode = "browse"; renderAreaSelect();
      return [closed, search, els["area-select"].has("hidden")];""")
    assert out == [[True, True, False], True, False]


@_node
def test_the_counts_are_read_for_the_state_on_screen_and_a_late_answer_for_another_state_is_dropped(tmp_path):
    out = _run(tmp_path, """useDomains(); factsMode = "browse"; factsState = "stale";
      routes = { "/api/user/facts/domain-counts": { total: 1, counts: { transport: 1 } } };
      await loadAreaCounts();
      const stale = [requests.map((r) => r.url), areaCounts, els["area-select-count"].textContent];
      factsState = "current"; areaCounts = null;
      const late = loadAreaCounts(); factsState = "stale"; await late;
      return [stale, areaCounts];""")
    assert out[0] == [["/api/user/facts/domain-counts?state=stale"],
                      {"state": "stale", "total": 1, "counts": {"transport": 1}}, "· 1"]
    assert out[1] is None


@_node
def test_choosing_an_area_closes_the_list_and_lists_that_area_through_browse(tmp_path):
    out = _run(tmp_path, """useDomains(); factsMode = "browse"; areaCounts = { state: "current", ...COUNTS };
      routes = { "/api/user/facts/browse": { facts: [FACTS[1]], next_cursor: null } };
      openAreaList(); setDomainFilter("money"); await settle();
      return { urls: requests.map((r) => r.url), open: areaListOpen, label: els["area-select-label"].textContent,
        count: els["area-select-count"].textContent, set: els["area-select-btn"].has("set"),
        reset: !els["area-reset"].has("hidden"), rows: els["facts-container"].innerHTML };""")
    assert out["urls"] == ["/api/user/facts/browse?limit=50&domain=money"]
    assert out["open"] is False and (out["label"], out["count"]) == ("Money", "· 1")
    assert out["set"] and out["reset"]
    assert "Alex&#39;s car insurance with Example Mutual Insurance" in out["rows"]


@_node
def test_a_new_state_starts_on_all_areas_and_reads_its_own_counts(tmp_path):
    out = _run(tmp_path, """useDomains(); factsMode = "browse"; browseDomain = "sport";
      areaCounts = { state: "current", ...COUNTS };
      routes = { "/api/user/facts/domain-counts": { total: 0, counts: {} }, "/api/user/facts/browse": { facts: [] } };
      setFactsState("stale"); await settle(); await settle();
      return [browseDomain, requests.map((r) => r.url).sort(), els["state-chips"].innerHTML];""")
    assert out[0] == "all"
    assert out[1] == ["/api/user/facts/browse?limit=50&state=stale", "/api/user/facts/domain-counts?state=stale"]
    assert re.search(r'data-state="stale"[^>]*class="on" aria-pressed="true"', out[2])


# ------------------------------------------------------------------------------------------------ the fact rows

@_node
def test_facts_are_rows_with_text_area_tag_and_date_that_open_to_edit_and_delete(tmp_path):
    out = _run(tmp_path, """useDomains(); 
      factsMode = "browse"; allFacts = FACTS.slice(); renderFacts(allFacts, true);
      return els["facts-container"].innerHTML;""")
    rows = re.findall(r'<div class="c-mem-item reveal" data-idx="(\d+)">(.*?)\n        </div>', out, re.S)
    assert [r[0] for r in rows] == ["0", "1", "2"]
    first = rows[0][1]
    assert 'onclick="toggleFactRow(this.dataset.idx)"' in first and 'aria-expanded="false"' in first
    assert "morning yoga 30 min" in first and '<span class="fact-domain-badge">Sport</span>' in first
    assert re.search(r"<span>[^<]*2026</span>", first)
    assert 'data-idx="0" onclick="openCorrectModal(this.dataset.idx)"' in first and ">Edit</button>" in first
    assert 'data-idx="0" onclick="openInvalidModal(this.dataset.idx)"' in first and ">Delete</button>" in first
    assert "fact-domain-badge" not in rows[2][1]                 # tennis has no profile yet: no area tag
    assert "style=" not in out


@_node
def test_a_row_opens_alone_and_closes_on_a_second_click(tmp_path):
    out = _run(tmp_path, """
      const items = [0, 1].map((i) => { const item = el("item-" + i), fact = el("fact-" + i);
        item.dataset.idx = String(i); item.querySelector = () => fact; return item; });
      els["facts-container"].querySelectorAll = () => items.filter((i) => i.has("open"));
      els["facts-container"].querySelector = (sel) => items.find((i) => sel.includes(`"${i.dataset.idx}"`));
      toggleFactRow("0"); const first = items.map((i) => i.has("open"));
      toggleFactRow("1"); const second = items.map((i) => i.has("open"));
      toggleFactRow("1");
      return [first, second, items.map((i) => i.has("open")), els["fact-1"].attrs["aria-expanded"]];""")
    assert out == [[True, False], [False, True], [False, False], "false"]


@_node
def test_edit_opens_the_correction_modal_and_copies_the_message_for_the_chat(tmp_path):
    out = _run(tmp_path, """allFacts = FACTS.slice(); openCorrectModal("0");
      els["modal-correct-new"].value = "evening swimming 45 min"; copyCorrectMessage(); await settle();
      return [modals[0].id, modals[0].body.includes("morning yoga 30 min"), clipboard];""")
    assert out[:2] == ["correct-fact", True]
    assert out[2] == ['I found this fact in my memory database and it needs correction.\nCurrent: "morning yoga 30 min"'
                      '\nCorrect: "evening swimming 45 min"\nPlease update it.']


@_node
def test_delete_invalidates_the_fact_removes_its_row_and_reads_the_counts_again(tmp_path):
    out = _run(tmp_path, """factsMode = "browse"; allFacts = FACTS.slice(); openInvalidModal("1");
      routes = { "/api/user/facts/domain-counts": COUNTS };
      await invalidateFactDirect(); await settle();
      return [modals[0].id, requests.map((r) => [r.method, r.url]), allFacts.map((f) => f.id), toasts];""")
    assert out[0] == "invalid-fact"
    assert out[1] == [["POST", "/api/user/facts/f-insurance/invalidate"],
                      ["GET", "/api/user/facts/domain-counts?state=current"]]
    assert out[2] == ["f-yoga", "f-tennis"] and out[3] == [["Fact deleted", "success"]]


@_node
def test_the_footer_says_how_many_of_the_areas_facts_are_shown_and_offers_more(tmp_path):
    out = _run(tmp_path, """factsMode = "browse"; areaCounts = { state: "current", ...COUNTS }; browseCursor = "f-tennis";
      allFacts = FACTS.slice(0, 2); renderFacts(allFacts, true);
      const browse = [els["facts-count"].textContent, els["facts-pagination"].innerHTML];
      factsMode = "search"; renderFacts(allFacts, true);
      return [browse, [els["facts-count"].textContent, els["facts-pagination"].innerHTML]];""")
    assert out[0][0] == "Showing 2 of 3" and 'onclick="browseNextPage()"' in out[0][1] and "Show more" in out[0][1]
    assert out[1] == ["Showing 2", ""]


@_node
def test_closed_facts_are_read_only_rows(tmp_path):
    out = _run(tmp_path, """useDomains(); factsMode = "browse"; factsState = "closed";
      routes = { "/api/user/facts/recently-closed": { window_days: 30, facts: [{ id: "c1",
        text: "Alex's car insurance with Example Mutual Insurance", primary_domain: "transport", state: "archived",
        closed_at: "2026-10-20T10:00:00Z" }] } };
      await closedLoad(); return [els["facts-container"].innerHTML, els["area-select"].has("hidden")];""")
    assert 'class="c-mem-item c-mem-static"' in out[0] and "No longer true" in out[0] and "Transport" in out[0]
    assert "<button" not in out[0] and "onclick" not in out[0]
    assert out[1] is True


# ------------------------------------------------------------------------------------------------ the review row

@_node
def test_the_review_questions_collapse_into_one_row_with_their_count(tmp_path):
    out = _run(tmp_path, """
      const q = (id) => ({ question_id: id, text: "plays tennis on Sundays", group: "questions", kind: "still_true",
        status: "waiting", waiting_since: "2026-10-01T00:00:00Z", options: [{ answer: "still_true", label: "Yes" }] });
      routes = { "/api/user/facts/review-queue": { questions: [q("q1"), q("q2"), q("q3")] } };
      await loadReviewQuestions();
      const shown = [els["memory-review"].has("hidden"), els["memory-review-count"].textContent,
        els["memory-review-title"].textContent, els["memory-review"].has("open"),
        els["review-questions"].innerHTML.includes("data-question-id")];
      toggleMemoryReview();
      const opened = [els["memory-review"].has("open"), els["memory-review-row"].attrs["aria-expanded"]];
      routes = { "/api/user/facts/review-queue": { questions: [] } }; await loadReviewQuestions();
      return [shown, opened, els["memory-review"].has("hidden")];""")
    assert out[0] == [False, "3", "Alek has questions about your memory", False, True]
    assert out[1] == [True, "true"]
    assert out[2] is True


# ------------------------------------------------------------------------------------------------ the memory inbox

INBOX_CARD = {
    "candidate_id": "c1", "text": "Clara booked the Coimbra weekend", "happens_when": "March 6-8", "said_when": "",
    "basis": "said", "source": "Desk assistant", "received": "2026-11-25T11:30:00+00:00",
    "draft": {"directive": False, "temporal_class": "ephemeral", "context_priority": "medium",
              "expiry": {"kind": "date", "date": "2026-03-08"}},
    "relation": {"kind": "refines", "fact_text": "Clara has a trip <b>in March</b>"},
    "raw": [{"information": "Clara booked Coimbra", "basis": "inferred", "said_when": "", "happens_when": "",
             "received": "2026-11-25T11:30:00+00:00"}],
}
INBOX_NEXT = {**INBOX_CARD, "candidate_id": "c2", "text": "Alex trains with Robin", "relation": {"kind": "new",
              "fact_text": None}}
# The stand-in DOM does not fill a field from the markup: `useInbox` loads the list and `typed` puts the card's own values
# into the fields, as the browser does, so a test changes only what the owner would.
_INBOX_ROUTE = ("routes = { '/api/user/memory-inbox': { candidates: [INBOX_CARD, INBOX_NEXT], confirmed: 1 } };"
                "async function useInbox() { factsMode = 'browse'; await loadMemoryInbox(); requests.length = 0; typed(); }"
                "function typed(over = {}) { const c = inboxCandidates[0]; els['inbox-text'].value = c.text;"
                " els['inbox-happens'].value = c.happens_when; els['inbox-said'].value = c.said_when;"
                " Object.entries(over).forEach(([id, v]) => { els[id].value = v; }); }")


def _run_inbox(tmp_path: Path, body: str):
    return _run(tmp_path, f"const INBOX_CARD = {json.dumps(INBOX_CARD)}, INBOX_NEXT = {json.dumps(INBOX_NEXT)};\n"
                          f"{_INBOX_ROUTE}\n{body}")


@_node
def test_the_inbox_is_one_collapsed_row_with_its_count(tmp_path):
    out = _run_inbox(tmp_path, """
      await useInbox();
      const shown = [els["memory-inbox"].has("hidden"), els["memory-inbox-count"].textContent,
        els["memory-inbox-title"].textContent, els["memory-inbox"].has("open")];
      toggleMemoryInbox();
      const opened = [els["memory-inbox"].has("open"), els["memory-inbox-row"].attrs["aria-expanded"]];
      routes = { "/api/user/memory-inbox": { candidates: [], confirmed: 0 } }; await loadMemoryInbox();
      return [shown, opened, els["memory-inbox"].has("hidden"), els["memory-inbox-body"].innerHTML];""")
    assert out[0] == [False, "2", "From your assistants", False]
    assert out[1] == [True, "true"]
    assert out[2] is True and out[3] == ""


@_node
def test_a_card_edits_four_fields_and_shows_the_rest_read_only(tmp_path):
    html = _run_inbox(tmp_path, """await useInbox(); return els["memory-inbox-body"].innerHTML;""")
    assert re.search(r'<textarea id="inbox-text"[^>]*>Clara booked the Coimbra weekend</textarea>', html)
    assert 'id="inbox-happens"' in html and 'value="March 6-8"' in html and 'id="inbox-said"' in html
    assert len(re.findall(r"<input ", html)) == 2 and len(re.findall(r"<textarea ", html)) == 1
    basis = re.findall(r'data-basis="(\w+)"[^>]*class="(on)?" aria-pressed="(\w+)"', html)
    assert basis == [("said", "on", "true"), ("document", "", "false"), ("inferred", "", "false")]
    assert "From Desk assistant, received" in html and "2026" in html
    assert "Memory would likely file it as: short-lived, medium priority, holds until" in html
    assert "Refines what memory holds: \u201cClara has a trip &#60;b&#62;in March&#60;/b&#62;\u201d" in html
    assert "<b>" not in html
    assert re.search(r"<details><summary>What was sent</summary>.*Clara booked Coimbra", html, re.S)
    assert "<details open" not in html


@_node
def test_a_source_name_with_replacement_patterns_renders_literally(tmp_path):
    html = _run_inbox(tmp_path, """
      INBOX_CARD.source = "Desk $' and $` assistant";
      INBOX_CARD.relation = { kind: "refines", fact_text: "Trip $' and $`" };
      await useInbox(); return els["memory-inbox-body"].innerHTML;""")
    assert "From Desk $&#39; and $` assistant, received" in html
    assert "Trip $&#39; and $`" in html


@_node
def test_true_sends_only_the_edited_fields_and_the_card_leaves_the_list(tmp_path):
    out = _run_inbox(tmp_path, """
      await useInbox();
      authFetch = async (url, init) => { requests.push({ url, method: init.method, body: init.body });
        return { ok: true, status: 200, json: async () => ({ status: "confirmed" }), clone() { return this; } }; };
      typed({ "inbox-text": " Clara booked the Coimbra weekend, March 6-8 " }); setInboxBasis("document");
      await answerInbox("confirm");
      const first = [requests.map((r) => [r.method, r.url, r.body]), inboxCandidates.map((c) => c.candidate_id),
        els["memory-inbox-body"].innerHTML.includes("Alex trains with Robin"),
        els["memory-inbox-body"].innerHTML.includes("2 confirmed, waiting"), els["memory-inbox-count"].textContent];
      typed();
      await answerInbox("confirm");
      return [first, requests[1].body, els["memory-inbox"].has("hidden")];""")
    assert out[0][0] == [["POST", "/api/user/memory-inbox/c1/confirm",
                          '{"text":"Clara booked the Coimbra weekend, March 6-8","basis":"document"}']]
    assert out[0][1:] == [["c2"], True, True, "1"]
    assert out[1] == "{}" and out[2] is True


@_node
def test_not_true_rejects_without_a_body_and_a_refused_edit_keeps_the_card(tmp_path):
    out = _run_inbox(tmp_path, """
      await useInbox();
      const answers = [{ status: 400, error: "too_long" }, { status: 200 }];
      authFetch = async (url, init) => { requests.push({ url, method: init.method, body: init.body });
        const a = answers.shift();
        return { ok: a.status === 200, status: a.status, json: async () => (a.error ? { error: a.error } : { status: "rejected" }),
          clone() { return this; } }; };
      await answerInbox("confirm");
      const kept = [inboxCandidates.length, toasts.slice(), els["inbox-true"].disabled];
      await answerInbox("reject");
      return [kept, requests.map((r) => [r.url, r.body]), inboxCandidates.map((c) => c.candidate_id), inboxConfirmed];""")
    assert out[0] == [2, [["The text is too long: shorten it.", "error"]], False]
    assert out[1] == [["/api/user/memory-inbox/c1/confirm", "{}"], ["/api/user/memory-inbox/c1/reject", None]]
    assert out[2:] == [["c2"], 1]


@_node
def test_a_locked_tab_shows_nothing_of_the_inbox(tmp_path):
    out = _run_inbox(tmp_path, """
      await useInbox(); forgetFacts();
      const forgotten = [els["memory-inbox"].has("hidden"), els["memory-inbox-body"].innerHTML];
      renderMemoryInbox();
      return [forgotten, els["memory-inbox"].has("hidden"), els["memory-inbox-body"].innerHTML];""")
    assert out == [[True, ""], True, ""]


@_node
def test_there_is_no_batch_control(tmp_path):
    html = _run_inbox(tmp_path, """await useInbox(); return els["memory-inbox-body"].innerHTML;""")
    assert "checkbox" not in html and 'type="radio"' not in html and "select-all" not in html
    labels = re.findall(r"<button[^>]*>(.*?)</button>", html, re.S)
    assert labels == ["You said it", "A document says it", "Inferred", "True", "Not true"]
    assert re.findall(r"onclick=\"(\w+)\(", html) == ["setInboxBasis"] * 3 + ["answerInbox"] * 2
    assert "Memory still decides what to keep" in html


# ------------------------------------------------------------------------------------------------ strings

def _dicts():
    body = re.search(r"const LANG_DICT = \{(.*?)\n      \};", _STRINGS_JS, re.S).group(1)
    out = {}
    for lang in LANGS:
        block = re.search(rf"\n        {lang}: \{{(.*?)\n        \}},", body, re.S).group(1)
        out[lang] = dict(re.findall(r'^\s*"([\w.]+)": "((?:[^"\\]|\\.)*)",?$', block, re.M))
    return out


def test_every_string_the_section_uses_exists_in_every_language():
    used = set(re.findall(r't\("((?:memory|questions)\.[\w.]+)"\)', _MEMORY_JS))
    assert {"memory.all_areas", "memory.areas_hidden", "memory.areas_overlap", "memory.review_row", "memory.edit",
            "memory.delete", "memory.shown_of"} <= used
    dicts = _dicts()
    assert [(lang, k) for lang in LANGS for k in sorted(used) if not dicts[lang].get(k)] == []


def test_the_new_strings_are_not_english_copies():
    dicts = _dicts()
    new = [k for k in dicts["en"] if k in {"memory.all_areas", "memory.area_filter", "memory.areas_hidden",
                                           "memory.areas_overlap", "memory.review_row", "memory.fact_hint",
                                           "memory.shown", "memory.shown_of", "memory.more", "memory.empty"}]
    assert len(new) == 10
    assert [(lang, k) for lang in ("uk", "fr", "es") for k in new if dicts[lang][k] == dicts["en"][k]] == []


def test_the_counts_route_is_read_through_the_guarded_fetch():
    assert "factsFetch(\n            `${API}/api/user/facts/domain-counts?state=${encodeURIComponent(state)}`" in _MEMORY_JS


_INBOX_STRING_KEYS = (
    ["row", "confirmed", "from", "field_text", "field_happens", "field_said", "field_basis", "class", "directive",
     "expiry_none", "expiry_date", "expiry_horizon", "rel_new", "rel_refines", "rel_contradicts", "rel_duplicates",
     "raw", "true", "not_true", "note", "err_too_long", "err_empty", "err_gone", "failed"]
    + [f"basis_{b}" for b in ("said", "document", "inferred")]
    + [f"temporal_{c}" for c in ("permanent", "stable", "dynamic", "ephemeral")]
    + [f"priority_{p}" for p in ("critical", "high", "medium", "low", "archival")])


def test_the_inbox_strings_exist_in_four_languages():
    dicts = _dicts()
    keys = [f"memory.inbox_{k}" for k in _INBOX_STRING_KEYS]
    assert [(lang, k) for lang in LANGS for k in keys if not dicts[lang].get(k)] == []
    for key, holes in {"memory.inbox_from": ("{source}", "{date}"), "memory.inbox_confirmed": ("{n}",),
                       "memory.inbox_class": ("{what}",), "memory.inbox_expiry_date": ("{date}",),
                       "memory.inbox_expiry_horizon": ("{days}",)}.items():
        assert [(lang, h) for lang in LANGS for h in holes if h not in dicts[lang][key]] == []
    for kind in ("refines", "contradicts", "duplicates"):
        assert [lang for lang in LANGS if "{fact}" not in dicts[lang][f"memory.inbox_rel_{kind}"]] == []
    used = set(re.findall(r't\("(memory\.inbox_[\w]+)"\)', _MEMORY_JS))
    assert [(lang, k) for lang in LANGS for k in sorted(used) if not dicts[lang].get(k)] == []
