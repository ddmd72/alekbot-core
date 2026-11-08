# Lelik voice modes v1: interpreter and Spanish tutor

**Status:** Done (v1)

## Decision

A web call can run in a **mode** besides the companion call. v1 has two: `interpreter` and `tutor`
(`VoiceMode` in `src/domain/voice_mode.py`; `lelik` is the default companion call). A mode is a
single-purpose call with its own prompt profile, started from its own page. Modes are xAI-only.

## How it works

- **Entry.** Each mode has its own page, `/cabinet/call/<mode>`, with its own web manifest
  (`/cabinet/call/<mode>/manifest.webmanifest`, `start_url` = that page), so a Home Screen icon opens
  that mode directly. `/cabinet/call/modes` lists the modes (own manifest too). The mode page is
  `call.html` with the manifest link swapped and `window.VOICE_MODE` set; after a login it returns to
  itself. The main call page `/cabinet/call` is unchanged. All of these routes are public like the
  existing manifest and icons; the API calls behind them need the Cabinet JWT.
- **Request path.** `POST /api/voice/web-call` takes an optional `mode` in the body (default `lelik`).
  An unknown value answers 400 `unknown_mode`. The mode goes through
  `VoiceCallSetupService.prepare` to `LelikAgent.session_config(mode=...)`, which builds the prompt
  from `VoiceProviderProfile.prompt_profile_for(mode)`. `prepare` stores the mode in the ticket and in
  the call-kind record.
- **Profiles.** `lelik_xai_interpreter` and `lelik_xai_tutor` are `lelik_xai` with the `voice_persona`
  token swapped (`VOICE_PERSONA_INTERPRETER`, `VOICE_PERSONA_TUTOR_ES`) and `LANG_ROLE_FIRST` in the
  `output_language` slot. Both are `non_overridable`, so a user's fixed-language override never reaches
  a mode, while chat keeps it. Tokens that serve only the plain call's MCP tools (`VOICE_TOOL_DATA_LELIK`,
  `LELIK_VOICE_MCP_RFC.md` §6) are left out of the mode profiles: a mode gets no MCP tools (O7).
- **Providers.** Only xAI has `mode_profiles`. OpenAI has no `voice_persona` slot, so
  `prompt_profile_for` raises `UnsupportedVoiceMode`; `prepare` releases the marker and ticket without
  an alert and the route answers 409 `mode_unavailable` (the page shows a distinct message).
- **Summary.** `/voice/submit-transcript` reads the mode back from the call-kind record, keyed by the
  ticket. Modes outside `VOICE_MODES_WITH_CALL_SUMMARY` (only `lelik`) get no after-call summary to
  chat and memory: a session of a tool is not a conversation to remember. Billing and the marker
  release are unchanged. A missing or expired record (a call over one hour) falls back to the summary.
- **Phone.** Phone calls stay in the default mode.

### Interpreter behaviour

Universal: the caller's language is the one they speak on the call, and the other language is learned
from the other person's first words. Without a press of the **"Ask Lelik"** button Lelik only translates:
everything either side says, even nonsense, even words that sound meant for him (owner, 2026-11-08). The
button (interpreter page only, `VOICE_CALL_CONTROL_CHANNEL_RFC.md` §5) marks the caller's **next utterance**
with a system note: those words are for Lelik (setup such as where they are or which language to use, repeat,
clarify), and he answers the caller in their language instead of translating. The button stays pressed until
Lelik's answer ends; a second press before speaking cancels it; it releases itself after 60 s without speech.
This replaces the name trigger ("Lelik, …"), which failed live on the first utterance and whenever the name was
not the first word, and the rule for the other person speaking to Lelik directly. The prompt change ships as a
token upload after the relay with the button is live (RFC §12).

## Alternatives rejected

- **Mode buttons on the main call page.** The owner wants that page to stay as it is, and a button
  there invites accidental taps on a paid call.
- **The user prompt override.** USER overrides are user-wide: the language swap leaked into chat.
  Non-overridable mode profiles keep the mode inside the call.
- **A provider speed setting for the tutor's pace.** The owner wants pace controlled by the prompt,
  with reminders added later.

## Not in v1 / next

- Switching mode mid-call, and entering or leaving a mode by voice.
- On-screen text.
- The tutor's lesson plan made outside the call and fed back from it.
- Periodic pace reminders.
- Modes as agent skills.

## Resumption (xAI only)

A call that drops (not one ended with the hang-up button) can be continued by a re-dial within 25 minutes in the same mode on
the same provider: the relay reports xAI's conversation id with the transcript, the main service keeps it per user
(`voice_resume:{user_id}`: handle, mode, provider; no transcript), and the next call's session config carries it, so the
adapter reconnects with `conversation_id` and Lelik opens with a resume note, in his own words and showing he is in the topic.
xAI raises no error for an unknown or expired id; the adapter detects an honoured resume by replayed items before
`session.updated`, else the call is fresh. Details and probe facts: `VOICE_CALL_CONTROL_CHANNEL_RFC.md` §6.

## Triggers to revise

A third mode, a second provider with a voice-persona slot, or a mode that should be remembered
(its summary then moves into `VOICE_MODES_WITH_CALL_SUMMARY`).
