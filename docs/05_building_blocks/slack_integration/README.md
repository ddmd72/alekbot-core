# Slack Integration (Building Block)

## 1. Overview

Slack reaches Alek-Core over the **HTTP Events API** — one transport, on Cloud Run.

A second transport (Socket Mode, a persistent WebSocket for local development) existed
until 2026-08-16 and was removed: it was never used once development moved to the cloud, and
it was the only path that ran without session persistence. See
[`decisions/slack_socket_mode_removal.md`](../../04_solution_strategy/decisions/slack_socket_mode_removal.md).

---

## 2. Architecture

To handle Slack's strict 3-second timeout and prevent double-processing, the HTTP mode uses an asynchronous worker pattern.

### 2.1 Event Pipeline

1. **Ingress:** Slack sends a webhook to `/slack/events`.
2. **Verification:** `HTTPModeAdapter` reads the raw body (capped at 1 MiB, else 413), verifies the `X-Slack-Signature` on those bytes using HMAC-SHA256 for every request type, `url_verification` included, and only then parses it (a body that is not a JSON object is 400).
3. **Enqueue:** The event is enqueued to **Google Cloud Tasks** under a deterministic task name derived from the `event_id`; `ALREADY_EXISTS` (Slack's retry) means it is already queued. There is no separate dedup store.
4. **Response:** The adapter immediately returns `200 OK` to Slack.
5. **Worker:** Cloud Tasks triggers the `/worker` endpoint; `WorkerHandler` claims the delivery by task name (a redelivery is skipped, `decisions/worker_task_dedup.md`) and executes the `ConversationHandler`. The lock key is the Slack thread (`thread_ts`, else the message `ts`), released by a first turn at the 90 s long-turn mark. A busy thread waits up to 100 s, then the event is re-queued under a named task (max 12 x 15 s), then a "resend" notice is posted; a redelivery that finds the lock busy re-queues at once.

### 2.2 Blueprint Pattern

The Slack adapter implements the **Blueprint Pattern** (Quart), allowing it to share port 8080 with other services (OAuth, Telegram, User Cabinet).

- **Prefix:** All Slack routes are prefixed with `/slack`.

---

## 3. Security & Authorization

### 3.1 Signature Verification

Every HTTP request is validated using the `SLACK_SIGNING_SECRET`.

- **Timestamp Check:** Prevents replay attacks by rejecting requests older than 5 minutes.
- **HMAC Validation:** Ensures the request originated from Slack. The check precedes any parsing, on `/slack/events` and `/slack/interactivity` alike, and both routes read the body through `request_body.read_capped_body`.

### 3.1.1 Literal text

Text the bot did not write (a stored fact quoted in the memory digest, a model-translated acknowledgement) is sent with `ResponseChannel.send_literal` (`send_reply` is literal too): on Slack `&`, `<` and `>` are escaped by `adapters/slack/mrkdwn_text.py` and `mrkdwn` is off, so `<!channel>` or `<@U1>` is shown, not obeyed; on Telegram `parse_mode=None`. `UserNotificationService.notify_raw(..., literal=True)` is the notification path to it.

### 3.2 IAM Integration

Every event (message or mention) triggers an `iam_service.authorize("slack", slack_user_id)` call.

- **Unauthorized Users:** Receive a registration link to the Web UI.
- **Authorized Users:** Proceed to the multi-agent reasoning loop.

---

## 4. Code References

- `src/composition/slack_adapter_factory.py`: Builds the adapter (lives in `composition/` — creates `ConversationHandler` and injects it as a port).
- `src/adapters/slack/http_adapter.py`: Webhook implementation.
- `src/adapters/slack/response_channel.py`: Slack-specific message formatting.
- `src/adapters/gcp_task_queue.py`: Integration with Cloud Tasks.

---

## 5. Status & Roadmap

**Status:** ✅ Production Ready

### Planned Enhancements

- **Interactive Components:** Support for Slack buttons, menus, and modals.
- **App Home:** Implement a personalized dashboard within the Slack app.
- **Slash Commands:** Add native commands for quick actions (e.g., `/remember`, `/search`).

---

**Last Updated:** 2026-08-16
**Status:** ✅ Complete  
**Phase:** Documentation Audit Phase 3.6
