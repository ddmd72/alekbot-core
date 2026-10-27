---
category: lifecycle
class: lifecycle
metadata:
  description: 'ConsolidationAgent review pass (Facts S4 §3.10): how to decide each
    stale fact (the outcome order, the bases, history rewrites, invoices, the reply
    mapping, asking the owner with folds and the fallback, the checklist line, the
    final report)'
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

    in_this_pass: "In this pass two taxonomy lines are replaced: the horizon line that counts N from reported_date, and the date rule that discards a past plan or rewrites it as history. Here a horizon counts from now, not from reported_date; a fact whose date has passed is rewritten as history (as_history), never archived or discarded, unless the reply or evidence says it moved. Every other date rule still applies (a range -> its last day; only a month -> its last day). The taxonomy's lines that point to Domain_Scope (a reply to a memory-review question) are for consolidating a conversation (Stage 1); here a reply is question.reply, decided by the reply mapping below."

    when_unsure: "When unsure, ask: call ask_owner. The fact stays visible while it waits for the owner's answer, so asking loses nothing. Never revive, archive or invalidate a fact nothing in front of you decides."

    outcome_order: [
        "Decide each one by the first of these that applies:",
        "(1) a current fact states the same thing: supersede it by that fact;",
        "(2) the reply or evidence says it still holds: revive it with that basis (rewrite its text if the text changed);",
        "(3) its date has passed: rewrite it as history (basis evidence or reply, or date_passed when its own text already reports the outcome); otherwise, when the owner's reply says it is over: archive it; it was never true: invalidate it;",
        "(4) anything else: ask_owner.",
        "A fact whose question is open or timed out is not asked again; when nothing decides it, its outcome is leave.",
        "Every listed fact gets exactly one outcome."
    ]

    calls: {
        supersede: "update_fact(fact_id, updates={state: SUPERSEDED, superseded_by: <the current fact's id>}). superseded_by names a current fact, or a listed fact this pass made current; it is the reason, so send no basis. It never names a fact this pass rewrote as history on date_passed. The parts of one plan are folded with ask_owner folds, not superseded."
        revive: "update_fact(fact_id, updates={state: CURRENT, expiry: <the new decision>}, basis=<evidence or reply>)."
        rewrite: "a revive whose text changed: update_fact(fact_id, updates={state: CURRENT, content: <the new text>, expiry: <decided from the new text>}, basis=<evidence or reply>); send tags, metadata or temporal_class only when they change too."
        history: "update_fact(fact_id, updates={state: CURRENT, content: <the history text>, expiry: <kind none>}, basis=<evidence, reply or date_passed>, as_history=true). The history text: English, past tense, at most 40 words; keep the date at the precision the fact had (a day, a range, a month), what and where; drop identifiers (booking and invoice numbers, PINs, codes, times, seat and baggage detail); an invoice or a payment keeps its amount and due day. Phrase it one of three ways: happened (\"In Nov 2026 the user had the car serviced (Nov 4).\"), planned and did not happen (\"The user planned to attend a conference Nov 18-20, 2026, and did not go.\"), planned and unknown (\"The user planned to attend a concert on Dec 5, 2026; whether it took place is unknown.\")."
        archive: "update_fact(fact_id, updates={state: ARCHIVED}, basis=<reply>): only when the owner's reply says it is over; evidence and date_passed never back an archive."
        invalidate: "update_fact(fact_id, updates={state: INVALIDATED}, basis=<evidence or reply>)."
        ask: "ask_owner(fact_id, question, texts, fallback, folds?, noise?): see asking."
    }

    basis: {
        evidence: "{kind: evidence, evidence_fact_id}: another current fact shows it (a neighbour, or a fact this pass made current, except one it rewrote as history on date_passed), never a version of the same fact. A neighbour that reports the outcome decides the history: the car service on Nov 4 found worn brake pads -> it happened; sold the concert tickets -> it did not."
        reply: "{kind: reply, question_id, quote}: the owner's reply to this fact's question; quote is words copied from that reply."
        date_passed: "{kind: date_passed, date}: the fact's date (YYYY-MM-DD) is before today. It backs only a history rewrite, and only of what the fact's own text already reports as done (an invoice received, a payment made, a subscription cancelled with access until a day); it never says that a planned event took place or did not, nor that an invoice was paid. A fact of expiry kind date uses its own date: leave date out."
    }

    invoices: "An invoice is not a payment: a payment is confirmed only by a neighbour (a payment fact, or an email or a receipt that became a fact) or by the owner. A neighbour that confirms the payment: rewrite it as history that it was paid, basis evidence. A routine invoice with no confirmation: rewrite it as history on date_passed as \"An invoice for <what> (<amount>), due <day>, was received; payment not confirmed.\" and send payment={paid: <the history text if it was paid>, not_paid: <the history text if it was not>}; code lists it in the owner's weekly digest, where he ticks the paid ones. A fine or a debt: ask_owner with question paid."

    expiry: [
        "A revive and a rewrite carry expiry, decided from the fact's content with the kinds table: {kind: date, date: YYYY-MM-DD}, {kind: horizon, days: N} or {kind: none}.",
        "Horizons count from now: {kind: horizon, days: N} means review again in N days (N ≥ 2).",
        "{kind: none} says the fact holds for good: give it only when evidence or the owner's reply says so, or with a history rewrite.",
        "An archive, an invalidation and a supersede carry no expiry.",
        "reported_date is kept by the system: do not send it."
    ]

    reply_mapping: {
        what_a_reply_is: "A reply is the owner's reply to the question message, in words, shown as the fact's question.reply (a digit never reaches the review: code applies it). Every call it backs takes basis {kind: reply, question_id, quote}."
        still_true: "still true -> revive."
        stopped_or_ended: "stopped or ended -> archive. For a standing fact, also send history: one sentence in the past tense saying it ended and when the owner said so; code creates it as its own fact with {kind: none}. A fact of expiry kind date is not archived: rewrite it as history (as_history) on basis reply. Send no history when a current fact already says it ended."
        outcome: "it happened, it did not happen, or it is no longer needed -> a history rewrite (as_history) on basis reply, in the matching phrasing."
        renewal: "renewed -> a rewrite to the renewed state on basis reply (code sets its expiry); not renewed -> a rewrite to the current state on basis reply, with {kind: none}."
        never_or_no: "never true, or a bare no -> invalidate."
        correction: "a correction -> invalidate, or supersede by the current fact that states the true value."
        dont_remember: "don't remember -> the question's fallback, now, basis reply: revive (with an expiry), archive, or for fallback history a history rewrite in the unknown phrasing."
        another_subject: "about something else -> no answer: decide the fact from the rest of what is in front of you; if nothing decides it, its outcome is leave, and its question stays open."
    }

    asking: [
        "ask_owner(fact_id, question, texts, fallback): one listed fact with no result in this pass and no open question. Code writes the question and sends it later, one at a time; you write no question of your own.",
        "question: still_true (the default: is it still true?), happened (a plan or an event whose date passed: a fact of expiry kind date, or a horizon plan such as a pickup or a callback), renewed (a validity end that changes the current fact: a document, a permit, a membership, a trial), paid (a fine or a debt whose due day passed).",
        "texts: the facts code writes for each answer, all in this one call, with no count_words call: happened -> happened, not_happened, unknown; renewed -> renewed, not_renewed, not_needed, unknown; paid -> paid, not_paid, unknown; still_true -> none. Each is one line in English, at most 40 words, with no [, written by the text rules of a history rewrite; renewed and not_renewed state the current fact, the others are history; unknown says the outcome is unknown (for paid: payment not confirmed). The texts describe the whole plan.",
        "fallback is what happens if the owner does not answer: history (its unknown text is written) for happened, renewed and paid; revive for still_true; archive only with noise: true when the fact reads as noise, not as something the owner said. A fact of expiry kind date is asked as happened, renewed or paid, or as still_true with archive and noise: true.",
        "folds: the other listed facts that are parts of the same plan (the flights, the hotel and the transfer of one trip), each with no result and no open question; ask about one part and fold the rest; code closes each fold in favour of the asked fact once the question is queued.",
        "An asked or folded fact takes no other result in this pass.",
        "A question with timed_out: true is shown once: decide the fact now if a neighbour created since decides it; otherwise its outcome is leave, and its fallback applies after this pass. Do not ask it again."
    ]

    checklist: [
        "Before any call, write one line per listed fact, in the order listed: <fact_id> | neighbour_says=<id|none> | reply=<yes|no|over|never|unclear|none> | history=<evidence|own_text|ask|none> | outcome=<…>.",
        "neighbour_says: the id of a current fact that states the same thing or decides the fact; none when no neighbour does.",
        "reply: yes (still true), over (stopped or ended), never (never true, or a correction), no (a bare no), unclear (don't remember, or about something else), none (no reply).",
        "history: for a fact whose date has passed (a fact of expiry kind date, or a horizon plan whose day is before today): evidence when a neighbour reports the outcome, own_text when its own text already reports what was done, ask when neither does; none for any other fact.",
        "outcome: supersede, revive, rewrite, history, archive, invalidate, ask_owner or leave, from the first step of outcome_order that applies; leave is a fact whose question is open or timed out and that nothing decides: make no call for it."
    ]

    refusals: "A refused call wrote nothing: fix what the refusal names and send the call again, or leave the fact. When the refusal says the fact changed outside the review, leave it."

    final_report: "After your last call, end with one JSON object: {\"operations\": [{\"fact_id\": \"<id>\", \"outcome\": \"<outcome>\"}], \"message_language\": …, \"labels\": …, \"translations\": …}. Give message_language, labels and translations only when the message's LANGUAGE or TRANSLATIONS section asks for them, exactly as it says."
}
