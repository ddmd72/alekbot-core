# Spike 0.6 — Callback round trip: ~10s to reconnect, 2/2 machine detections correct

**Date:** 2026-09-21
**Status:** Live (spike complete, real call data)

## Question

RFC §4.6 makes callback the whole identity mechanism: a dial from a bound number gets hung up and
called back, so possession of the line — not a spoken credential — is what authenticates. Two
things had to be verified before this is buildable as designed (§7, §9 item 7): is the round trip
(hang up → wait → phone rings again) tolerable in practice, and does Twilio's answering-machine
detection (AMD) reliably tell a person from voicemail — an unreliable AMD result means a voicemail
greeting could get summarized into the owner's long-term memory (§4.9).

## Method

`scripts/voice/test_callback_roundtrip_poc.py` — a Quart webhook (`/auth` answers the inbound dial
with `<Say>`+`<Hangup>` and originates the outbound callback with
`machine_detection="DetectMessageEnd"`, `async_amd=False`; `/answer` reads `AnsweredBy` and either
proceeds or hangs up without opening a session; `/status` logs call state transitions). Real calls
from the owner's bound number to the provisioned Twilio number, exposed via `ngrok http 8766`,
`NO_PROXY='*'` (Charles Proxy MITM, same known issue as every other script this session).

## Result

**3 "person answers" trials** — the owner dialed in, hung up on the auto-answer, and picked up the
callback normally each time:

| trial | `AnsweredBy` | `gap_since_inbound` |
|-------|-------------|----------------------|
| 1 | human | 11.42s |
| 2 | human | 9.73s |
| 3 | human | 9.89s |

Mean ≈ 10.3s, tight spread (9.7–11.4s). Owner's own subjective read: "мгновенный" (instant) —
the measured gap and the felt experience agree.

**2 genuine voicemail trials** (both required the owner to manually force the phone to redirect to
voicemail — see the note below on why):

| trial | `AnsweredBy` | `gap_since_inbound` | outcome |
|-------|-------------|----------------------|---------|
| 1 | machine_end_beep | 16.73s | correctly hung up, no session opened |
| 2 | machine_end_beep | 16.74s | correctly hung up, no session opened |

**4 additional attempts produced `status: no-answer` with no `AnsweredBy` at all** — the owner's
phone line has voicemail disabled by default, so simply not answering the callback just times out
unanswered; there is nothing for AMD to detect because the call never actually connects to
anything. This is not a failed test of AMD — it's a real, useful finding in its own right (see
Verdict).

> **Correction (2026-09-22, first deployed call):** those four were **not** unanswered time-outs.
> A time-out takes the full ring timeout (55 s). They ended within 1–3 s with **SIP 480
> Temporarily Unavailable** from the carrier (Twilio call events, `sip_response_code`), and the
> first real call reproduced it. This script originated the callback from inside `/auth`, so it
> sometimes reached the handset while the ~2 s inbound dial was being torn down. A callback that
> lands during the inbound call instead shows as a second incoming call, which is what the owner
> saw on the successful trials. The fix is to originate on the inbound dial's `completed` status
> (RFC §4.6).

**Infra note, same class as spike 0.2's:** updating the Twilio number's Voice Configuration via
the classic REST API (`IncomingPhoneNumbers.voice_url`) did not reliably take effect for live call
routing during this session, even though re-reading the same API confirmed the write succeeded and
`date_updated` was current — the owner had to set the webhook URL manually in the Console UI for
it to actually apply to a live call. Not root-caused this session (possibly the newer Console UI
writes through a different config path than the classic 2010-04-01 API); worth keeping in mind for
Slice 1's actual number provisioning, where this would need to be either automated reliably or
accepted as a manual one-time setup step.

## Verdict

**Round trip is tolerable — under the §3-stated 5-15s ballpark, and confirmed by the owner's own
felt experience, not just the number.** §4.6 proceeds as designed on this axis; no need to fall
back to the shelved `<Gather>` DTMF mechanism.

**Machine detection is reliable when actually exercised — 2/2 correct in this sample.** The
caveat is real: n=2 is small, and both had to be manually forced because the owner's line doesn't
have voicemail enabled by default, so this doesn't yet cover the more realistic "someone's phone
naturally sends an unanswered call to voicemail after N rings" case end to end — this session
could only test "the call already reached an active voicemail greeting," not "the ring timeout
correctly triggers voicemail pickup in the first place," since that second step depends on carrier
and phone settings outside Twilio's control entirely. On the numbers actually measured, though,
`MachineDetection=DetectMessageEnd` did not misfire in either direction (no false "human" on a
machine pickup, no false "machine" on the person-answer trials) — §4.6 proceeds as designed on
this axis too, with the sample-size caveat carried forward.

**Net: §4.6 as written in the RFC holds.** No redirect to `<Gather>` DTMF is warranted by this
data.

## Revisit if

- **Sample size is small (n=3 human, n=2 machine).** If Slice 1's real usage surfaces a
  misdetection, this is the first thing to re-run at a larger sample before assuming the mechanism
  itself needs to change.
- **The "natural ring-to-voicemail" path was never tested** — only "voicemail already active" was.
  If the owner's phone (or a future bound number's phone) has voicemail enabled by default, that
  natural path should be tested once, since it's the realistic production case, not the forced one
  used here.
- **The Twilio Console API-vs-UI propagation gap** (see infra note above) needs root-causing
  before Slice 1 automates number provisioning — if it recurs, Slice 1's deploy process needs a
  manual verification step, not just an API call, until the cause is understood.
