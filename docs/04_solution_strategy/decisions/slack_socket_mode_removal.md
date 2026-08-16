# Slack Socket Mode removed

**Date:** 2026-08-16
**Status:** Done

## Decision

Slack Socket Mode is gone: `SocketModeAdapter`, the `SlackMode` enum, `SLACK_MODE`,
`is_socket_mode` / `is_http_mode`, the `SLACK_APP_TOKEN` / `DEV_SLACK_*` settings, and
`run_dummy_server()` (socket's stand-in health endpoint). The HTTP Events API is the only
Slack transport.

Socket Mode existed for local development without a public HTTPS endpoint. Development moved
to the cloud, and it has not been used since; it was also the only path that constructed an
adapter with no session store, so it kept a second, weaker wiring alive for nobody.

## Why now

The removal was forced into the open by a trap, not by tidiness: `_detect_slack_mode()`
defaulted to **socket for development**, so deleting only the adapter would have left dev with
no Slack adapter at all. Mode detection had to go with it — there is no half-removal here.

## Alternatives rejected

- **Keep the adapter, drop only the default** — leaves a one-member enum, a mode concept with
  one mode, and dead wiring the next reader has to disprove.
- **Keep Socket Mode for offline development** — nothing runs offline anymore; Firestore,
  Cloud Tasks and GCS are all remote, so the WebSocket bought nothing on its own.
- **Deprecate and delete later** — a banner is not a plan, and the mode-detection default made
  the code actively misleading in the meantime.

## Consequences

- `SlackAdapterFactory.create_adapter` no longer branches; `db_client` is unconditionally
  required and raises without it.
- `slack_bolt` stays — `base.py` and `http_adapter.py` use it.
- Local development against Slack now needs a tunnel to the webhook endpoint, or use Telegram.
- REQ-CORE-09 (Adapter Mode Selection) narrows from "select between two modes" to "HTTP with
  its dependencies"; `tests/unit/test_req_core_09_adapter_selection.py` covers the new shape.

## Revisit if

An offline or air-gapped development mode becomes necessary again — restore from git history
rather than reintroducing mode detection.
