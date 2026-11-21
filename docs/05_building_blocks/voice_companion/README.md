<!-- living-doc
covers:
  src/services/voice_session_service.py
  src/ports/realtime_session_port.py
  src/adapters/openai_realtime_adapter.py
  src/adapters/xai_realtime_adapter.py
  src/agents/lelik_agent.py
  src/agents/lelik_summarizer_agent.py
  src/agents/alek_gateway_agent.py
  src/services/lelik_persona_service.py
  src/domain/lelik_context.py
  src/services/voice_resume_service.py
  src/composition/voice_resume_setup.py
  src/domain/voice_resume.py
  src/services/call_text_file_service.py
  src/domain/call_text_file.py
  src/domain/call_text_markdown.py
  src/domain/voice_mode.py
  src/domain/voice_language_pair.py
  src/domain/voice_ask_latch.py
  src/domain/voice_live_text_pacer.py
  src/domain/voice_errand.py
  src/domain/voice_delegation_outcome.py
  src/domain/voice_call_note.py
  src/domain/voice_call_buffer.py
  src/domain/voice_provider_profile.py
  src/domain/voice_session_spec.py
  src/domain/voice_turn_ownership.py
  src/domain/voice_mcp_servers.py
  src/domain/remote_tool_server.py
  src/domain/result_links.py
  src/assets/voice/
  scripts/voice/ask_note_probe.py
  scripts/voice/test_callback_roundtrip_poc.py
  scripts/voice/test_late_function_call_output_poc.py
  scripts/voice/test_reasoning_effort_poc.py
  scripts/voice/test_xai_mcp_shape_probe.py
  scripts/voice/test_xai_realtime_protocol_poc.py
-->
# Voice Companion (Lelik)

## Purpose

Lelik is the voice of the exocortex: one realtime speech-to-speech conversation with the owner, over a phone call or a
web call. He talks from a warm prompt (the owner's ordinary facts, the primary chat's recent history, date and place),
and goes to Alek, the text system, only for what the prompt does not hold: mail, tasks, documents, the web, actions.
This block is the call itself: the session loop that sits between the audio and a realtime provider, the two providers
behind one port, what a call is given and may do, its modes, and what it leaves behind.

What it is not. The audio transport (call admission, Twilio, the SFU, the control channel) is not described here (see [Voice Call Transport](../voice_call_transport/README.md)). Lelik is
not a text agent and not a delegation target: he is `internal` in the manifest and never offered to Smart.

Two processes carry the block. The **relay** (`relay_main.py`, its own Cloud Run service) runs
`VoiceSessionService`, holds the provider socket and the audio, and keeps no state beyond the call. The **main service**
holds everything durable and every model call except the realtime one: it builds the session, runs delegations, bills,
summarises and delivers. The two meet only on the OIDC-protected `/voice/*` routes.

## Owned data

Nothing in this block has a collection of its own. A call's state is short-lived keys in the voice tickets store
(`FirestoreEphemeralStore`, `EnvironmentConfig.voice_tickets_collection`), plus what the call finally writes into
chat history, drive and billing.

- **Ticket record** (`voice_ticket_keys.ticket_key`) and **call-kind record** (`call_kind_key`): the assembled session config
  (instructions with the owner's facts, tools, `voice` spec, MCP descriptors without tokens, mode, call kind, ids) and
  `{call_kind, mode}`. Written by `VoiceCallSetupService.prepare`. The ticket is **consumed once** by
`/voice/session-config` (`get_and_delete`), because it carries the facts. The call-kind record is read at call end (twice:
  `submit_transcript` and the summary consumer) and by every delegation; missing means companion.
- **Delegation result and abandoned markers** (`result_key`, `abandoned_key`, TTL `_LATE_ANSWER_TTL_S`): how an answer the
  relay stopped waiting for still reaches chat, exactly once (see Delegation).
- **Resume record** `voice_resume:{user_id}` (`src/domain/voice_resume.py::resume_key`): the provider's conversation
  handle, the mode and the provider, expiring with `RESUME_TTL_S`. Never a transcript. Written by
  `VoiceResumeService.keep`, read and consumed by `VoiceResumeService.take`.
- **Button-end marker** (`button_end_key`): the caller ended with the hang-up button; such a call is never resumed.
- **Call buffer** (`VoiceCallBuffer`): relay memory only, flushed once by `submit_transcript`.
- **Chat history**: a call that ends with a note appends two messages to the primary session, the event line and the note
  (`UserNotificationService.notify_call_summary`). That pair is the only durable trace of a companion call; Lelik has no
  memory store and writes no facts.
- **Billing and observability**: per-model usage through `QuotaService.record_usage`; one `prompt_content` row per turn
  (agent type `lelik`, the provider the relay reports with the call-end report, `unknown` from an older relay), both
  written at call end.

## Flows

### Preparing a call

2. `session_config` fails closed: if the persona context or the prompt cannot be built the call does not open (no empty
   prompt). The context is the ordinary facts of the user's `voice_fact_domains`, no standing directives and the primary
   channel's recent history (`lelik_warm_context.md`).
3. The provider is the user's `voice_provider` or the default (`resolve_voice_profile`). One `VoiceProviderProfile` carries
   what differs per provider; its `VoiceSessionSpec` rides in the session config under `voice`, so the relay never guesses.
4. The interpreter declares no tools; every other mode gets `delegate_to_specialist` and `send_to_chat`. With
   `LELIK_XAI_NATIVE_SEARCH` on, a natively searching provider loses the delegated light search.

### The session loop

`VoiceSessionService.handle_call` redeems the ticket, pairs MCP servers with their tokens and enters `_run_call`, which opens
the provider session from `relay_main.py::_realtime_session_factory` (it refuses an adapter that does not implement the
spec's turn ownership). Teardown order is fixed and matters: cancel the loops, close the live text and the Ask latch, abandon
unheard delegations, close the provider session, then `submit_transcript` in a `finally`, because that report also releases
the one-call marker and revokes the call's MCP tokens.

**Turn ownership** (`TurnOwnership`, per call, from the spec) decides who speaks when:
- *RELAY* (OpenAI): the provider never replies by itself (`create_response` off). The relay starts every reply in
  `_start_response` right after the caller's turn with the persona anchor (`build_persona_anchor`) ahead of it, owns
  barge-in, sends the caller opening and the silence and waiting notes.
- *PROVIDER* (xAI, the default): the provider replies and stops when talked over. The relay places the anchor once at the
  start (and again after a mid-call note, `_mid_call_note`), starts only the greeting and replies to delegation answers,
  and on barge-in only drops queued audio. The debounce `barge_in_min_speech_s` and the silence watchdog do not apply.

On every call (web, phone, resumed; either ownership) `_forward_inbound` drops the caller's audio until the relay has
requested the greeting, so the provider never takes the silent line before it as input and Lelik always speaks first
(`decisions/voice_greeting_holds_caller_audio.md`).

No `if provider ==` lives in the service. What differs is declared on the class: `supported_turn_ownership` and
`late_tool_result_after_caller_turn` on `RealtimeSessionPort`, and the `update_session` and `mcp_servers` capabilities,
which an adapter that cannot do them refuses (`NotImplementedError`, `ValueError`).

**Barge-in** (relay-owned) is `_barge_in`: read what was heard first, then clear the audio, cancel the response only if one
is active, then truncate the item to the heard part. The order is load-bearing (see Invariants).

### Delegation

Lelik has two tools. `delegate_to_specialist` reaches `search_memory`, `search_web_light` and `ask_alek`; `send_to_chat`
is a channel, not a worker (decision `lelik_send_to_chat.md`).

1. `_start_delegation` maps the tool call to delegation arguments
   (`src/domain/voice_errand.py::delegation_arguments`: `send_to_chat(request)` becomes the errand intent of the same
   name), attaches the call's recent exchanges as `call_context`, and runs it as a tracked task.
2. The task posts to `/voice/delegate` and waits up to the `delegation_timeout_s` set in `relay_main.py::main`. The route
   runs `LelikAgent.delegate_outcome`, which refuses an intent outside `permitted_intents`, then dispatches like any text
   orchestrator's delegation. It runs shielded from the HTTP handler, because the relay disconnects exactly when it stops
   waiting.
3. `ask_alek` goes to `AlekGatewayAgent`: zero LLM, it routes the commission to the user's Router so Smart answers with
   memory. Its timeout `ASK_ALEK_TIMEOUT_MS` deliberately outlasts the relay's wait.
4. `send_to_chat` is the errand: ASYNC (a Cloud Task), the gateway posts Alek's whole answer to chat, Lelik hears only the
   acknowledgement. Errands are not abandoned and get no waiting notes; a failed one is reported by `AgentWorkerHandler`.
5. A late answer (the caller spoke since dispatch) goes in as a system note on OpenAI, which drops a late
   `function_call_output`, and as the `function_call_output` on xAI, which ignores a late note
   (`late_tool_result_after_caller_turn`). The reply is started only when the floor is free; otherwise it is owed
   (`_reply_or_owe`, `_reply_if_owed`).

**Late answers.** If the relay times out or the call ends with answers unheard, it posts `/voice/delegate/abandon`; the
two routes write the result marker and the abandoned marker in opposite orders and the post is claimed by an atomic
`get_and_delete` on the result key (`_post_claimed`), so the answer reaches chat once, whichever side is second. A late
failure never reaches chat in any mode (owner rule); the interpreter drop holds with the `VOICE_MCP_TOOLS` knob on or off.

### Modes

`VoiceMode` has the companion call (`lelik`) and two single-purpose modes started from their own web page: `interpreter`
and `tutor`. Both are xAI-only (OpenAI has no `mode_profiles`), non-overridable by the user's language setting (per
`lelik_voice_modes.md`; the profiles live in Firestore), and get no after-call summary to chat or memory (`VOICE_MODES_WITH_CALL_SUMMARY`). Phone calls stay in
reaches them), and get no after-call summary to chat or memory (`VOICE_MODES_WITH_CALL_SUMMARY`). Phone calls stay in
the companion mode. The mode travels in the ticket and in the call-kind record.

**Interpreter.** Declares no tools and carries no MCP server: the owner's rule is that a tool that only translates must have
nothing a mistranslated phrase could act on. The provider's own web and X search is offered only with `LELIK_XAI_NATIVE_SEARCH`
on. Every utterance gets a system note placed on `speech_started`: a translate note, or an ask note when the caller pressed
"Ask Lelik" (`AskLatch`). While a cycle is open the relay offers one tool, `set_language_pair`, through `update_session`;
`_handle_language_pair` accepts a call only when code agrees (two different codes of a closed set, a marked turn committed
since the press, none accepted since). An accepted pair also goes into the session instructions as a standing block,
because a note placed at `speech_started` can land late when speech begins in the tail of Lelik's playback.

**Tutor.** The same session with another persona token; it keeps the delegation tools and carries no MCP server.

**Live text** (both modes, with a control link): `LiveTextPacer` holds Lelik's transcript until the audio it belongs to has
played and, on barge-in, drops what was not heard; see the live text RFC.

**Geo.** A position the caller shares on purpose becomes one system note (`_handle_geo`; rules in the geo marks RFC).

### MCP tools of a call

Only the plain companion call on xAI (`voice_mcp_eligible`) with the `VOICE_MCP_TOOLS` knob on carries a remote MCP server,
the drive (index reads only, results cut at a voice cap). Memory is not an MCP server of any call; it goes by the
`search_memory` delegation. `LelikAgent.session_config` puts the descriptor (no token) on the config;
`/voice/session-config` mints a per-call token at redemption (never at prepare, never stored) and returns it; the relay
pairs token and descriptor, checking the token's audience against the server's URL, and the adapter hands the entry to xAI,
which calls the server itself. Teardown deletes the live-call record, so the token stops verifying at hang-up, not at expiry.
The provider runs these calls without the relay: the adapter reports `remote_tool_done`, and since xAI starts no follow-up
reply on its own, `_note_remote_tools` and `_reply_if_owed` start it.

### Ending a call

`submit_transcript` (the same blueprint) records usage per model, drops a voicemail (`is_voicemail`: the answering-machine
verdict or the caller's words; a voicemail keeps no resume handle), keeps the resume handle unless the call ended by the button, and, for companion calls only,
calls the summary consumer in `main.py`: `CompanionExtractorRunner` runs `LelikSummarizerAgent` as a separate cheap-tier
model, never Lelik, and `notify_call_summary` posts the note and appends the event pair to history
(`call_event_from_turns` writes the event line). A summary failure raises an ops alert; without it the failure would be
silent. In `finally` it flushes the prompt-content store (Cloud Run throttles CPU after the response), revokes the MCP
tokens and releases the one-call marker.

### Resume

xAI keeps a conversation server-side. The adapter always asks for `resumption`; the relay reports the handle with the
end-of-call report; a re-dial within `RESUME_TTL_S`, same user, mode and provider, gets it from `/voice/session-config`
(`VoiceResumeService.take` consumes it) under `RESUME_CONFIG_FIELD`, and the adapter reconnects with `conversation_id`. xAI
raises no error for an unknown id, so "resumed" means a replayed item arrived before `session.updated`
(`XaiRealtimeAdapter.resumed`); a resumed call gets a resume note and neither the caller opening nor the pickup note.

### Keeping a call as a file

An interpreter or tutor web page posts the call's lines to `/api/voice/call-text/file`; `CallTextFileService.save` summarises
(a failed summary still saves the file, untitled) and delivers Markdown to chat or the drive Inbox. It never raises.

## Invariants and traps

- **Barge-in reads before it clears.** Twilio echoes the marks of dropped audio after a clear, so clearing first counts
  unheard audio as played and truncates the item too late. `_barge_in` also guards `cancel_response` on an active response:
  a cancel with nothing in flight is a provider error that ends the call, and so does any provider `error` event.
  `tests/unit/services/test_voice_session_service.py::test_barge_in_reads_heard_audio_then_clears_cancels_and_truncates_in_order`.
- **`response.create` during an active response is a call-ending error.** `_start_response` claims the slot before its
  first `await`; answers wait in `queued_answers` and are replied to once. Never add a second starter.
- **A tool call of a barged-in response is answered, not run.** Its `function_call` still needs an output or it dangles,
  but running it spends a Smart turn on half a question. `tests/unit/services/test_voice_session_cancelled_tool_call.py`.
- **The interpreter rule does not depend on the `VOICE_MCP_TOOLS` knob.** `/voice/delegate` reads the call-kind record
  for every delegation (`_run_delegation`), and the late-answer drop for an interpreter (`_post_claimed`) relies on the
  mode that delegation stored. `tests/unit/web/test_voice_interpreter_rule_without_the_knob.py`,
  `tests/unit/agents/test_lelik_interpreter_no_tools.py`, `tests/unit/agents/test_lelik_delegate_intent_allowlist.py`.
- **Only the companion call leaves a note.** Interpreter and tutor sessions are tools, not conversations to remember:
  `VOICE_MODES_WITH_CALL_SUMMARY` holds `lelik` alone. `tests/unit/domain/test_voice_mode.py`.
- **The model id is a billing key, and an unpriced model bills at zero without an error.** `calculate_realtime_cost`
  returns 0 for an unknown model, so a bump of `_MODEL` in either adapter must come with its price; the guard tests
  fail instead of the bill. `tests/unit/domain/test_billing_realtime.py`. The main service deploys before the relay so a
  relay reporting a new model id never meets a main service without its price. xAI's `billable_audio_seconds` is a session
  running total; the adapter emits only the increment, because `VoiceCallBuffer` sums.
- **Tokens never reach a log.** The MCP token is held only by the session object; exceptions and provider text pass through
  `redact_exception` / `redact_for_log` with the call's secrets. A descriptor never carries a token
  (`RemoteToolServer.to_dict`).
- **A system note is never deleted.** A delete of a missing item ends the call, so the anchor and notes stay.
- **The call event line is written as `[System: ...]` into history** (`call_event_text`), a deviation from the repo-wide
  rule that code-written text never claims the system's voice. It exists so Alek does not mistake the note for his own
  reply. Do not copy the shape.
- **The `SPOKEN_DELIVERY` token must exist** (decision `lelik_delivery_single_source.md`): the anchor's spoken paragraph in
  `src/domain/llm.py::build_persona_anchor` is gated on its section. A token ships by upload, not by deploy.
- **No inline fallback prompt.** The summarizer fails closed when its prompt is missing, which is why the end-of-call summary
  has an alert. `tests/unit/services/test_lelik_summarizer_has_no_fallback.py`.
- **A resume handle is consumed once, belongs to one mode and provider, and is never logged or kept after a button
  hangup.** `tests/unit/services/test_voice_resume_service.py`, `tests/unit/services/test_voice_session_resumption.py`.

## Links

- RFCs: [`VOICE_COMPANION_RFC.md`](../../10_rfcs/VOICE_COMPANION_RFC.md) (the session loop, delegation, §4.15 tools, §4.16
  xAI), [`VOICE_MULTI_PROVIDER_RFC.md`](../../10_rfcs/VOICE_MULTI_PROVIDER_RFC.md) (turn ownership, profiles),
  [`VOICE_CALL_LIVE_TEXT_RFC.md`](../../10_rfcs/VOICE_CALL_LIVE_TEXT_RFC.md),
  [`LELIK_VOICE_MCP_RFC.md`](../../10_rfcs/LELIK_VOICE_MCP_RFC.md),
  [`VOICE_CALL_CONTROL_CHANNEL_RFC.md`](../../10_rfcs/VOICE_CALL_CONTROL_CHANNEL_RFC.md) (§5 Ask Lelik, §6 resume).
- Decisions: [`lelik_voice_modes.md`](../../04_solution_strategy/decisions/lelik_voice_modes.md),
  [`lelik_warm_context.md`](../../04_solution_strategy/decisions/lelik_warm_context.md),
  [`lelik_send_to_chat.md`](../../04_solution_strategy/decisions/lelik_send_to_chat.md),
  [`lelik_delivery_single_source.md`](../../04_solution_strategy/decisions/lelik_delivery_single_source.md),
  [`response_summary_in_character.md`](../../04_solution_strategy/decisions/response_summary_in_character.md),
  [`voice_xai_grok_voice_3.md`](../../04_solution_strategy/decisions/voice_xai_grok_voice_3.md),
  [`voice_xai_protocol_probe.md`](../../04_solution_strategy/decisions/voice_xai_protocol_probe.md),
  [`billing_cache_write_and_voice_seconds.md`](../../04_solution_strategy/decisions/billing_cache_write_and_voice_seconds.md).
- Probes behind the protocol facts: `scripts/voice/test_late_function_call_output_poc.py`,
  `scripts/voice/test_xai_realtime_protocol_poc.py`, `scripts/voice/ask_note_probe.py`. Read one before changing an adapter.
- Neighbours: [user drive](../user_drive/README.md) (drive MCP tools), [remote MCP server](../remote_mcp_server/README.md).

## Where to start reading

1. `src/services/voice_session_service.py::VoiceSessionService._run_call`: open, loops, teardown, report.
2. `src/services/voice_session_service.py::VoiceSessionService._consume_events`: every provider event and its rule.
3. `src/ports/realtime_session_port.py::RealtimeSessionPort`: what a provider must be, and the event vocabulary.
4. `src/agents/lelik_agent.py::LelikAgent.session_config`: what a call is given (prompt, tools, spec, MCP).
5. `src/web/voice_control_plane_app.py::create_voice_control_plane_blueprint`: the main service's half of a call.
