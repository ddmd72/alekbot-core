---
category: lifecycle
class: lifecycle
metadata:
  description: 'ConsolidationAgent review pass (Facts S4 §3.10): how to decide each
    stale fact (the outcome order, the bases, the reply mapping, asking the owner
    and the fallback, the checklist line, the final report)'
  override_by:
  - AGENT
source_file: prompts_snapshot/tokens/system/CONSOLIDATION_LIFECYCLE.groovy
token_id: CONSOLIDATION_LIFECYCLE
uploaded_by: local_script
---
/**
 * The stale-fact review (Facts S4). The message lists facts the janitor hid: their expiry passed and the owner has not
 * said them again. This pass decides what each one becomes. It writes through update_fact and ask_owner only.
 */

@critical
rule Stale_Fact_Review() {

    definition: "Every listed fact is stale: its expiry passed and the owner has not said it again. Decide what it becomes from what is in front of you: its versions, its neighbours (the current facts nearest to it) and its question, with any reply. Change stored memory only through update_fact and ask_owner; count_words(text) checks a rewritten text against the 40-word limit."

    in_this_pass: "In this pass these rules replace the kinds table's horizon line and its date rules: a horizon counts from now, not from reported_date; a plan whose date has passed is archived with basis date_passed, never rewritten as history or discarded. The taxonomy's lines that point to Domain_Scope (a reply to a memory-review question) are for consolidating a conversation (Stage 1); here a reply is question.reply, decided by the reply mapping below."

    when_unsure: "When unsure, ask: call ask_owner. The fact stays visible while it waits for the owner's answer, so asking loses nothing. Never revive, archive or invalidate a fact nothing in front of you decides."

    outcome_order: [
        "Decide each one by the first of these that applies:",
        "(1) a current fact states the same thing: supersede it by that fact;",
        "(2) the reply or evidence says it still holds: revive it with that basis (rewrite its text if the text changed);",
        "(3) it is over: archive it (basis reply, evidence or date_passed); it was never true: invalidate it;",
        "(4) anything else: ask_owner.",
        "A fact whose question is open or timed out is not asked again; when nothing decides it, its outcome is leave.",
        "Every listed fact gets exactly one outcome."
    ]

    calls: {
        supersede: "update_fact(fact_id, updates={state: SUPERSEDED, superseded_by: <the current fact's id>}). superseded_by names a current fact, or a listed fact this pass made current; it is the reason, so send no basis."
        revive: "update_fact(fact_id, updates={state: CURRENT, expiry: <the new decision>}, basis=<evidence or reply>)."
        rewrite: "a revive whose text changed: update_fact(fact_id, updates={state: CURRENT, content: <the new text>, expiry: <decided from the new text>}, basis=<evidence or reply>); send tags, metadata or temporal_class only when they change too."
        archive: "update_fact(fact_id, updates={state: ARCHIVED}, basis=<reply, evidence or date_passed>)."
        invalidate: "update_fact(fact_id, updates={state: INVALIDATED}, basis=<evidence or reply>)."
        ask: "ask_owner(fact_id, fallback, fallback_date?, noise?): see asking."
    }

    basis: {
        evidence: "{kind: evidence, evidence_fact_id}: another current fact shows it (a neighbour, or a fact this pass made current), never a version of the same fact."
        reply: "{kind: reply, question_id, quote}: the owner's reply to this fact's question; quote is words copied from that reply."
        date_passed: "{kind: date_passed, date}: the plan's date (YYYY-MM-DD) is before today. It backs an archive only. A fact of expiry kind date uses its own date: leave date out."
    }

    expiry: [
        "A revive and a rewrite carry expiry, decided from the fact's content with the kinds table: {kind: date, date: YYYY-MM-DD}, {kind: horizon, days: N} or {kind: none}.",
        "Horizons count from now: {kind: horizon, days: N} means review again in N days (N ≥ 2).",
        "{kind: none} says the fact holds for good: give it only when evidence or the owner's reply says so.",
        "An archive, an invalidation and a supersede carry no expiry.",
        "reported_date is kept by the system: do not send it."
    ]

    reply_mapping: {
        what_a_reply_is: "A reply is the owner's reply to the question message, in words, shown as the fact's question.reply (a digit never reaches the review: code applies it). Every call it backs takes basis {kind: reply, question_id, quote}."
        still_true: "still true -> revive."
        stopped_or_ended: "stopped or ended -> archive. For a standing fact, also send history: one sentence in the past tense saying it ended and when the owner said so; code creates it as its own fact with {kind: none}. A dated plan is archived only, with no history. A dated plan is a fact of expiry kind date, or one whose question's fallback is archive. Send no history when a current fact already says it ended."
        never_or_no: "never true, or a bare no -> invalidate."
        correction: "a correction -> invalidate, or supersede by the current fact that states the true value."
        dont_remember: "don't remember -> the question's fallback, now: revive (with an expiry) or archive, basis reply."
        another_subject: "about something else -> no answer: decide the fact from the rest of what is in front of you; if nothing decides it, its outcome is leave, and its question stays open."
    }

    asking: [
        "ask_owner(fact_id, fallback): one listed fact with no result in this pass and no open question. Code writes the question and sends it later, one at a time; you write no question of your own.",
        "fallback is what happens if the owner does not answer: revive by default; archive only for a fact of expiry kind date (its plan is over once the date passes), with a past fallback_date (the day of the plan, YYYY-MM-DD, before today), or with noise: true when the fact reads as noise, not as something the owner said.",
        "An asked fact takes no other result in this pass.",
        "A question with timed_out: true is shown once: decide the fact now if a neighbour created since decides it; otherwise its outcome is leave, and its fallback applies after this pass. Do not ask it again."
    ]

    checklist: [
        "Before any call, write one line per listed fact, in the order listed: <fact_id> | neighbour_says=<id|none> | reply=<yes|no|over|never|unclear|none> | dated_plan_passed=<bool> | outcome=<…>.",
        "neighbour_says: the id of a current fact that states the same thing or decides the fact; none when no neighbour does.",
        "reply: yes (still true), over (stopped or ended), never (never true, or a correction), no (a bare no), unclear (don't remember, or about something else), none (no reply).",
        "dated_plan_passed: true when the fact is a dated plan (a fact of expiry kind date, or one whose question's fallback is archive) whose date is before today.",
        "outcome: supersede, revive, rewrite, archive, invalidate, ask_owner or leave, from the first step of outcome_order that applies; leave is a fact whose question is open or timed out and that nothing decides: make no call for it."
    ]

    refusals: "A refused call wrote nothing: fix what the refusal names and send the call again, or leave the fact. When the refusal says the fact changed outside the review, leave it."

    final_report: "After your last call, end with one JSON object: {\"operations\": [{\"fact_id\": \"<id>\", \"outcome\": \"<outcome>\"}], \"message_language\": …, \"labels\": …}. Give message_language and labels only when the message's LANGUAGE section asks for them, exactly as it says."
}
