# Lelik's xAI voice model moves to `grok-voice-3.0`

**Date:** 2026-10-20
**Status:** Done

## Decision

`XaiRealtimeAdapter._MODEL` is `grok-voice-3.0` (was `grok-voice-think-fast-2.0`). xAI deprecated
Grok Voice 2.0; 3.0 keeps the endpoint, session params, events and price ($0.08 / min audio +
$0.004 / 1M text input, docs.x.ai/developers/models, checked 2026-10-20). Its model pages list only
`grok-voice-2.0` (deprecated) and `grok-voice-3.0`; our old id is not listed there.

## Why the explicit id, not the `grok-voice-latest` alias

The model id is a billing key (`_REALTIME_PRICING_PER_SECOND`). An alias moves silently: behaviour
changes with no commit, and a price change would be priced at the old rate.

## The fail-open pricing trap

`calculate_realtime_cost` returns 0.0 for an unpriced model, and the adapter reports `self._model` in
`response_done`. Changing `_MODEL` alone would bill every call at $0 with no error. Guard tests in
`tests/unit/domain/test_billing_realtime.py` assert the adapters' default models are priced, so the
next model bump fails a test instead of the bill.

## Rollout

The relay (sends `usage_by_model`) and the main service (prices it) are separate deploy units, so
either id can reach either revision. Both the 2.0 and 3.0 price keys stay for now. `make deploy`
deploys the main service before the relay (`cloudbuild-dev.yaml`), so a relay reporting 3.0 never
meets a main service without the 3.0 price; deploy the relay alone only after the main service.
Follow-up: remove the `grok-voice-think-fast-2.0` keys from `billing.py` after the rollout.

## Live probe (2026-10-20, scratchpad copy of `scripts/voice/test_xai_realtime_protocol_poc.py`)

- The production session shape (castor, server_vad 800 ms, `create_response`/`interrupt_response`
  on, effort `high`, one tool) gets `session.updated` with no `error`, and the echo names
  `grok-voice-3.0`. The echo omits `voice` and `interrupt_response`; 2.0 echoed `interrupt_response`.
- `billable_audio_seconds` is still the session's running total (7 → 13 → 20 over three replies).
- Barge-in over a long reply: the reply had already finished generating (faster than real time, as
  on 2.0), and the caller's turn got a new reply with no error.

## Open risk

The `lelik_xai` prompt was tuned on 2.0 calls (`voice_xai_protocol_probe.md`). Re-check Lelik's
register on an owner UAT call.

## Triggers to revise

An xAI price change, or a 3.0 regression on UAT.
