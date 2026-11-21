# Voice calls: the relay holds the caller's audio until the greeting is requested

**Status:** accepted. **Code:** `VoiceSessionService._forward_inbound` / `_open_inbound`, `_CallState.inbound_open`.

## Decision

The relay does not forward the caller's microphone audio to the realtime provider until it has requested the pickup
(greeting) response. Until then the inbound stream is still consumed, so the call's end is still seen and the transport
does not back up, but its frames are dropped. Applies to web, phone and resumed calls, relay-owned and provider-owned.
Owner's product rule: Lelik always speaks first.

## Rejected alternatives

- **Rewording the pickup note only.** The provider still received the caller's (often silent) audio as input while the
  greeting waited, and its first reply opened on that silence. Wording cannot undo what the provider has heard.
- **Buffering the held audio and flushing it after the request.** Flushed audio lands during the greeting and can be read
  as speech over Lelik, interrupting it.

## Trade-off

Words the caller says before the greeting is requested are lost, by design. On web the window is the wait for the page's
`listening` signal (capped); on phone it is the short time to open the session.

## Triggers to revise

A product change letting the caller speak first, or a provider that lets the relay mark input as ignored instead of
dropping it.
