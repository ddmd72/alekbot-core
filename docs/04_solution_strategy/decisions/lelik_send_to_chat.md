# Lelik: a worker and a channel — `send_to_chat` replaces `tell_alek`

**Date:** 2026-10-29 · **Decided by:** owner · **RFC:** `docs/10_rfcs/VOICE_COMPANION_RFC.md` §4.15 (delta 2026-10-29)

## Context

Since 2026-09-29 Lelik reached Alek through two intents of one tool, `delegate_to_specialist(intent=…)`:
`ask_alek` (a question; SYNC, Lelik waits and speaks the answer) and `tell_alek` (an errand; ASYNC, Alek posts
the outcome to chat and Lelik never hears back). The RFC rejected separate tools at the time, with the trigger
"Lelik confuses the two".

On a live call on 2026-10-29 Lelik chose `tell_alek` three times when the caller wanted the answer by voice, once
right after the caller asked for the answer by voice instead of in the chat. The answers went to chat and the call
went quiet.

The owner's diagnosis: the names do not carry the semantics. `ask` and `tell` both name the same worker, the verb
difference is weak for a realtime model, and as two enum values of one tool nothing in the declaration says
**where the answer goes**, which is the only thing that differs for the caller.

## Decision

Separate the worker from the channel.

- `ask_alek` stays an intent of `delegate_to_specialist`: a question **or an action** for Alek (set or change a
  reminder or task, save something). The answer comes back to Lelik, who says it or confirms the action by voice.
  Mechanics unchanged: SYNC, the relay waits up to 300 s, then (or at call end) abandons it and the main service
  posts the answer to chat.
- `tell_alek` is removed. The realtime session declares a second top-level tool, `send_to_chat(request)`: put
  something into the caller's chat; Alek finds or makes it and posts it there; Lelik does not hear back.
- Internally `send_to_chat` is the old errand path under the same word. The relay maps the tool call to the
  `send_to_chat` intent in one place (`src/domain/voice_errand.py`, `delegation_arguments`), keeps its errand
  handling (no dispatch filler, no waiting notes, no abandon), and forwards it through `/voice/delegate` to
  `LelikAgent.delegate_outcome`, which dispatches it like any delegation. The Alek gateway declares it ASYNC
  (a Cloud Task), posts the whole outcome to chat, and `AgentWorkerHandler` reports a failure. The intent stays
  internal: `LELIK.allowed_intents` does not name it (so the delegate tool never offers it) and Smart is never
  offered it. The run origin is `voice:send_to_chat`.
- Descriptions are one line each; the question/errand boundary rule is deleted from the prompts.

## Alternatives considered

1. **Rename the pair, keep both as intents** (`alek_answers_you` / `alek_answers_in_chat`). Fixes the names, but
   both remain values of one enum on a tool whose name is about workers; the channel is still a property of a
   string the model fills in, not of the tool it picks. Tool choice is the stronger signal for a realtime model,
   which is the one thing the incident showed is weak.
2. **One tool with an `answer_to` parameter** (`delegate_to_specialist(..., answer_to: "me" | "chat")`). The
   channel becomes explicit, but as an optional field the model must remember to set; a forgotten or defaulted
   field silently picks a channel. It also widens the shared delegate schema for one caller, and every other
   intent would carry a parameter that means nothing to it.
3. **A single `ask_alek`, Alek decides whether the answer goes to chat.** Fewest tools, and Alek is the stronger
   model. Rejected for now: it needs the relay's owed-reply path to work for answers that do not come back, and
   the defect "`response_owed` is set but never read" must be fixed first. Even then the caller's "not to the
   chat" would be judged twice (by Lelik, then by Alek). *Trigger to revisit:* that defect is fixed and
   `send_to_chat` is still mis-chosen.
4. **Keep `tell_alek` and add prompt rules or few-shots only.** The prompt already said when to use each; the
   failure was against the rule, not for want of one. A few-shot for "not to the chat, tell me" is added anyway,
   as support, not as the fix.

## Why

- A name that says where the answer goes (`send_to_chat`) and a tool boundary between "answer me" and "post it"
  give the model two different actions instead of two similar strings.
- Moving actions to `ask_alek` makes the caller hear the outcome ("done, set for nine"), which is what the
  incident's caller expected, and leaves `send_to_chat` for the one case where the chat is the point.
- Smallest change to the plumbing: the relay already knew the tool name of every call; the main service, the
  gateway, the Cloud Task and the failure notice keep their code paths under one renamed intent.

## Consequences

- Prompt files changed in `prompts_snapshot/` (`PROTOCOL_LELIK_DELEGATION`, `PROTOCOL_LELIK_DELEGATION_BARE`,
  `FEW_SHOT_EXAMPLES_LELIK`, `PROTOCOL_VOICE_PARTNER`, `PROTOCOL_DRIVE_NOTES`); they take effect only after an
  upload, which should go out with the deploy (the code no longer accepts `tell_alek`).
- A `tell_alek` Cloud Task still queued at deploy time is routed by its agent id to the gateway, which no longer
  recognises it as an errand: it runs as a question (`voice:ask_alek` origin) and only a links copy reaches the
  chat, and a failure gets no notice. Errands finish in minutes, so this is a deploy-window edge only.
- The xAI profile (`lelik_xai`, the default provider) does not load `FEW_SHOT_EXAMPLES_LELIK`; there the fix rests
  on the tool names and `PROTOCOL_LELIK_DELEGATION_BARE`.
- Older records (drive-notes, workspace, long-running-turns RFCs and decisions) say `tell_alek` /
  `voice:tell_alek`; read them as `send_to_chat` / `voice:send_to_chat`.
