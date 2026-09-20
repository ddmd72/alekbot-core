# Spike 0.5 — Provider-native context truncation for long realtime sessions

**Date:** 2026-09-20
**Status:** Live (spike complete, docs-only research; RFC not yet edited per plan's rule)

## Question

Voice Companion RFC §4.9 bets on "no in-call tiering in v1": a one-hour call (Cloud Run's
request-timeout ceiling, §4.14) "plausibly fits the 128K realtime context with no custom
mechanism," with the fallback "measure real token growth on the first long call; build tiering
only if the measurement demands it." §4.9 adds one caveat not yet resolved: "Provider-native
truncation, if offered, is checked first (§9)" — open question 6. This spike answers that check:
do OpenAI's or xAI's realtime/voice APIs expose a native mechanism (automatic truncation, rolling
summarization) that changes what "no custom mechanism" costs to rely on, or confirms it costs
nothing extra?

## Method

Read current provider docs directly (WebFetch) plus targeted WebSearch where direct URLs 404'd,
per Task 1's precedent that `developers.openai.com` is the live GA doc host
(`platform.openai.com/docs/api-reference/realtime-*` now 403s) and that xAI's `docs.x.ai` paths
are easier to find via search than by guessing. No live API calls — pure documentation research.

Pages read:
- `developers.openai.com/api/docs/guides/realtime-conversations` (realtime conversations guide)
- `developers.openai.com/api/reference/resources/realtime/client-events` (client events reference)
- `developers.openai.com/cookbook/examples/context_summarization_with_realtime_api` (cookbook, to
  separate native behavior from a documented custom pattern)
- `community.openai.com/t/what-does-auto-truncation-in-realtime-api-actually-do/1356153`
  (community thread, to check for undocumented mechanics behind the field)
- `docs.x.ai/docs/guides/voice/agent` (xAI Voice Agent / Speech-to-Speech guide)
- `docs.x.ai/developers/advanced-api-usage/context-compaction` (xAI's only "long context" feature,
  checked for realtime applicability)
- `developers.openai.com/api/reference/resources/realtime/subresources/sessions/methods/create`
  (attempted directly — 404; the field is documented on the client-events page instead, cross-checked
  via WebSearch snippets quoting the same reference)

## Result

### OpenAI — native truncation exists, default is already on

The GA session config has a **`truncation` field** (documented on the `client-events` reference
page, under the session object / `session.update` schema), with three forms:

- **`"auto"` — the default.** "When the number of tokens in a conversation exceeds the model's
  input token limit, the conversation will be truncated" by dropping the **oldest** messages
  first — a plain drop, not a summary. The community thread (no official OpenAI staff confirmation
  found, flagged as such) describes this at the token level: "removing the tail of oldest
  conversation tokens that cannot fit but preserving the initial re-run system message."
- **`"disabled"`** — turns truncation off; the server returns an error once the conversation
  exceeds the input token limit instead of pruning it.
- **`RealtimeTruncationRetentionRatio`** — `{"type": "retention_ratio", "retention_ratio": <0.0–1.0>,
  "token_limits": {"post_instructions": <int>}}`. Retains a fraction of post-instruction tokens
  (e.g. `0.8` drops messages until 80% of the max is used) specifically to "amortize truncations
  across multiple turns" and improve prompt-cache hit rate on the next turn.

Separately, the realtime-conversations guide states a **hard session ceiling: "The maximum
duration of a Realtime session is 60 minutes."** Not part of the truncation question, but directly
relevant to §4.9's "one-hour call" framing — worth a note for whoever next touches that section.

**What this is not:** no automatic summarization exists on OpenAI's side. The one summarization
pattern in OpenAI's own material — the `context_summarization_with_realtime_api` cookbook — is an
explicit, developer-written loop (poll `response.done` token counts, call a separate
`gpt-4o-mini` completion for a summary, `conversation.item.create` the summary,
`conversation.item.delete` the old turns) layered *on top of* `truncation`, not a variant of it.
The cookbook explicitly distinguishes the two: `truncation` "automatically optimizes context
truncation, preserving relevant information while maximizing cache hit rates" but the recipe still
writes custom summarization because dropping oldest turns outright is not the same as retaining
their meaning.

### xAI — no native mechanism for the realtime/voice endpoint

The Voice Agent / Speech-to-Speech guide (`docs.x.ai/docs/guides/voice/agent`) documents no
truncation field, no context-window limit, and no rolling-summarization behavior for
`wss://api.x.ai/v1/realtime`. The only duration-shaped fact in that guide is unrelated to in-call
context management: **"History is dropped after 30 minutes of inactivity"** under session
resumption — i.e. reconnecting to a *closed* connection past 30 minutes loses the cached replay
history; it says nothing about token pressure inside an open session.

xAI does have a **"Context Compaction"** feature (`docs.x.ai/developers/advanced-api-usage/
context-compaction`), but it is documented exclusively for the text/agentic surface: `POST
/v1/responses/compact` and chat-completion calls via the xAI SDK / OpenAI-compatible SDKs. It is
**manual, not automatic** — "a typical pattern is to call the Compaction API every N turns inside
an agent loop" — and nothing in that page states it applies to, or is reachable from, the realtime
voice endpoint. Consistent with Task 1's finding that xAI's realtime session schema is on an older,
narrower surface than OpenAI's GA schema (no `type` field, no forced text-only mode), it is
plausible this compaction feature simply hasn't been extended to the voice product; the docs do
not say either way, so this is reported as absence-in-the-checked-doc-section, not as a confirmed
"xAI will never support this."

No source found — OpenAI staff, cookbook, or xAI docs — describes any mechanism that fits
"rolling summarization" as a first-class API feature on either provider. Summarization, where it
exists at all (OpenAI's cookbook, xAI's text-only compaction), is always a manual pattern layered
on top of a separate primitive.

## Verdict

**Mixed outcome — does not cleanly fit either of the brief's two pre-written branches**, same
shape as Task 1's result: one provider offers something, the other doesn't, and even OpenAI's
"something" is a different mechanism than what §4.9 was hedging against.

- **OpenAI offers native truncation, and it is already the default — nothing to enable.** Unless
  Lelik's `session.update` payload explicitly sets `truncation` to `"disabled"` or a custom
  `retention_ratio`, the GA default (`"auto"`) already applies: if a call somehow runs long enough
  to exceed the model's input token limit, the session degrades by silently dropping its oldest
  turns rather than erroring the call out from under the caller. This costs zero implementation —
  it is standing behavior, not a feature to wire up. The one actionable item is negative, not
  additive: **Slice 1's session-config code must not touch `truncation`** (no `"disabled"`, no
  bespoke `retention_ratio` tuning) unless a deliberate reason is written down, since touching it
  is the only way to lose this free safety net.
  - This does **not** replace §4.9's own summary mechanism. `"auto"` truncation discards dropped
    turns outright (no digest of what was lost); §4.9's own end-of-call summarizer is a completely
    separate, already-planned pass over whatever the call-scoped buffer still holds. The two do not
    conflict — a mid-call `"auto"` truncation would only ever fire in the pathological case the
    128K bet is meant to make unnecessary, and even then it fails soft (drops history) rather than
    failing the call.
- **xAI offers nothing equivalent for the realtime endpoint.** No enable/disable decision applies
  because there is no field to decide about; if xAI is the chosen provider (open question 4,
  undecided) and a call genuinely exceeds its context window mid-conversation, current
  documentation gives no evidence of graceful degradation — worst case is an undocumented hard
  error, not verified here (out of scope: this spike is docs-only, no live overflow test was run
  on either provider).

**Net effect on §4.9's plan: no change needed.** "No in-call tiering in v1, measure real growth,
build only if demanded" was already the right call, and on the OpenAI leg it turns out to be
slightly better than the RFC assumed — the provider default already supplies an inexpensive
failure-soft backstop for the tail case the RFC wasn't otherwise protecting against, at zero
build cost, provided Slice 1 leaves `truncation` untouched. Open question 6 ("does 128K remove the
need for in-call tiering") is **still provisional on real token growth**, per the RFC's own
stated order — this spike answers only the "is there a native mechanism to check first" half of
§4.9's caveat, not the token-growth half, which stays gated on a real long call.

## Revisit if

- xAI is selected as the primary or a parallel provider (open question 4) and a real or
  synthetic long call is run against it — check empirically whether an xAI session that exceeds
  its context window errors, silently truncates, or behaves some other way; the docs are silent,
  so this can only be resolved by observing it.
- A real long call on OpenAI (§4.9's own "measure real token growth" step) shows truncation
  actually firing — at that point, evaluate `retention_ratio` explicitly (better cache economics
  across a long call) instead of leaving `"auto"`'s default oldest-drop behavior.
- xAI's documentation is updated to extend Context Compaction to the realtime/voice endpoint —
  re-check `docs.x.ai/developers/advanced-api-usage/context-compaction` and the Voice Agent guide.
