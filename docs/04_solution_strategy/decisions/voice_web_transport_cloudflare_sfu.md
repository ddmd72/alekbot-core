# Decision: Cloudflare Realtime SFU as the voice web transport

**Status:** Adopted
**Date:** 2026-09-27

## Context

`VOICE_COMPANION_RFC.md` §5.1 rejected a browser transport outright — it cannot work
screen-off on iOS Safari, which the phone path (Twilio callback) can. That rejection
stood, but it left two costs the owner no longer accepted once a screen-on browser call
became a real option (`docs/10_rfcs/VOICE_WEB_TRANSPORT_RFC.md` §1):

- **Money.** Every phone call is an outbound PSTN call to a Spanish mobile — roughly
  $0.05–0.19/minute (Twilio ES rates, 2026-09-27) before the realtime model itself.
- **Audio quality.** Twilio Media Streams is G.711 μ-law at 8 kHz both ways. The owner's
  verdict after the spike, hearing the same model over wideband PCM: "an order of
  magnitude better — the voice and intonation we could not get over Twilio."

A second transport only pays for itself if it (a) reuses `VoiceSessionService` and Lelik's
persona unchanged, (b) keeps the relay in the media path — VOICE_COMPANION §5's argument
for observability, barge-in and silence handling — and (c) stays inside Cloud Run's
constraints (no UDP).

## Spike results (2026-09-27, laptop, cloudflared tunnel, POC `scripts/voice/cloudflare_sfu_poc/`)

| Measure | Value |
|---|---|
| Browser ⇄ SFU | RTT 21–48 ms, jitter 2–4 ms, 0 loss, POP Madrid |
| SFU → relay (continuous, even in silence) | 50 msg/s, 3840 B frames → 0.68 GB/h |
| relay → SFU (continuous silence policy) | 0.64 GiB/h |
| First audio after end of speech | 646–1479 ms, median ~900 (Twilio: 1062–1939, median
  ~1350, plus PSTN setup) |
| Adapter setup + pull | ~2 s |
| Owner verdict | audio "ideal", voice/intonation far above Twilio |

**Cost** (verified rates): Cloudflare Realtime 1000 GB/mo free, then $0.05/GB; GCP Premium
egress $0.12/GiB after 1 GiB/mo. Worst case ≈ $0.077/h of GCP egress with Cloudflare still
inside its free tier — against Twilio's $3–11/h. Whether Cloudflare bills the WS PCM bytes
or the Opus bytes is still open (RFC §11) but irrelevant while under the free tier.

## Decision

Adopt Cloudflare's Realtime SFU as the second transport, dialing the **same relay** the
phone path uses over new `/sfu/ingest` and `/sfu/egress` WebSocket routes
(`SfuStreamHandler`), not a separate Cloud Run service. The SFU — not the browser — dials
the relay, mirroring Twilio's own topology: no UDP reaches Cloud Run, the relay stays the
one place all call audio and control flow are observable.

Main-service side mints SFU sessions over HTTPS with the App Secret
(`CloudflareSfuAdapter` behind `MediaRoomPort`); the relay resamples 48 kHz stereo PCM
(SFU wire format) to/from 24 kHz mono PCM (the OpenAI Realtime session), pacing outbound
audio through `PacedAudioOutlet` including continuous silence frames — the SFU
garbage-collects a track with no packets for 30 s.

**Pairing constraint, load-bearing:** both WebSocket legs of one call (ingest, egress)
carry the same ticket and must land on the same relay process to be paired in memory, so
the relay stays `--max-instances=1`. That was already the relay's deploy config; the SFU
transport makes it a **correctness** requirement, not just a cost choice. Scaling path if concurrent-call capacity is ever exceeded: N relay shards, each
still `--max-instances=1`, with the main service picking a shard per call (hash of user or
least-loaded) — not built (YAGNI), since capacity per instance is unmeasured until UAT.

`reasoning_effort` is set to `medium` on **both** voice paths as part of this change
(owner, 2026-09-27) — the phone path had been `high` since 2026-09-23 for the per-turn
prosody anchor; the web path adopts the same value as its default rather than starting
from a different one, so the two paths diverge only in transport, not in tuning, unless
UAT's `VOICE_WEB_REASONING_EFFORT` knob says otherwise.

## Alternatives rejected

- **Direct browser WebRTC → OpenAI, with a sideband control channel.** Zero transport
  cost, but the audio leaves Cloud Run's perimeter entirely (VOICE_COMPANION §5's
  observability argument), locks the browser to one provider's WebRTC surface, and would
  force `VoiceSessionService`'s barge-in/playback logic to be re-expressed over the
  provider's own WebRTC events instead of the relay-mediated model it uses today.
- **LiveKit Cloud.** Workable, but its free tier steps to $50/mo, the relay itself would
  count as a billed participant (doubling per-call minutes), and outbound WebRTC from
  Cloud Run was unproven going in — an unknown Cloudflare's HTTPS-fronted API avoids.
- **Self-hosted SFU on a VM.** Solves the UDP problem but reintroduces a VM, TLS/TURN
  termination and ops burden for a solo project; Cloud Run cannot take UDP directly, which
  is exactly what an SFU-in-front avoids needing.
- **Moving hosting to Cloudflare entirely.** Raised and set aside as unrelated to this
  feature — a hosting-platform decision, not a transport one.

## Explicit deferrals

- **Splitting `VoiceSessionService` into policy vs. audio-plumbing halves.** Only needed
  if audio ever bypasses the relay (i.e., the direct-WebRTC alternative above is revisited).
  Today the relay-mediated shape means the split buys nothing yet.
- **Converting `handle_call`'s callback bundle into a formal ABC transport port.** The
  bundle already behaves like a port structurally (`SfuStreamHandler` and
  `MediaStreamHandler` both drive it); making that literal touches ~100 lines across 15
  test files with no behavioural change, so it is deferred until a third transport makes
  the abstraction pay for itself.
- **Thinking-cue audio for non-μ-law transports.** The cue is already off in production
  for the phone path (cause of its 2026-09-24 reply-cutting bug not yet found); it is not
  built for PCM in the meantime and will be revisited together with re-enabling the cue
  itself, not separately per transport.

## Consequences

- Two call kinds now share one `VoiceSessionService`/persona/summary pipeline,
  distinguished only by `call_kind` (`phone`/`web`) threaded through the ticket — the
  pickup note and the summary header read correctly for either without a code fork per
  path.
- The relay gains no new secrets and no new Cloud Run unit; only the main service takes on
  two new optional secrets (`CLOUDFLARE_SFU_APP_ID`, `CLOUDFLARE_SFU_APP_SECRET`), and the
  new blueprint is skipped with a warning — not a boot failure — when they (or
  `VOICE_RELAY_STREAM_URL`) are absent, matching every other MVP/dev-only Voice Companion
  feature's graceful-absence convention.
- The `--max-instances=1` constraint is now doing double duty (cost + pairing
  correctness); raising it without also redesigning the pairing mechanism (e.g. a shared
  store keyed by ticket instead of an in-process dict) would silently break calls whose
  two legs land on different instances.
- Live verification (real browser call, barge-in, delegation, silence hangup, second call
  after marker release, iPhone Safari) is still pending — RFC status is "Implemented,
  pending UAT", not "Verified."

## Verification

Unit coverage (new files only, no existing test edited — see RFC §9 for the full list):
`AudioFormat`; `PlaybackTracker` at 48 B/ms; the OpenAI adapter's `audio/pcm` 24 kHz wire
shape; the SFU packet codec round trip; the resampler's tone preservation and
chunk-boundary continuity; `PacedAudioOutlet` framing/tail-padding/clear semantics;
`SfuStreamHandler` pairing, pairing timeout, egress re-attach, ingest-close-ends-call,
silence pacing; `CloudflareSfuAdapter`'s request shapes against mocked httpx;
`VoiceCallSetupService` claim/prepare/release; the web-call blueprint's auth, ownership,
busy (409), SFU-failure marker release, and status routes. `make check` (architecture
suite) must stay green throughout.
