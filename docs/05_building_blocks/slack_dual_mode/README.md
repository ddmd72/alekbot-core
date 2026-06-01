# Slack Dual Mode (Building Block)

## 1. Overview

Alek-Core supports two distinct modes for Slack integration, allowing for seamless transitions between local development and high-scale production environments.

**Core Principle:** The business logic remains identical; only the transport layer and lifecycle management change.

---

## 2. Integration Modes

### 2.1 Socket Mode (Development)

Used for local development and testing without requiring a public HTTPS endpoint.

- **Mechanism:** Maintains a persistent WebSocket connection to Slack.
- **Pros:** No firewall/NAT issues, instant feedback, easy debugging.
- **Cons:** Not suitable for multi-instance scaling, higher idle resource usage.
- **Adapter:** `SocketModeAdapter`.

### 2.2 HTTP Events API (Production)

Used for production deployment on serverless platforms like Cloud Run.

- **Mechanism:** Receives HTTPS POST requests (webhooks) from Slack.
- **Pros:** Stateless, scales to zero, industry standard for reliability.
- **Cons:** Requires public endpoint, signature verification, and async processing.
- **Adapter:** `HTTPModeAdapter`.

---

## 3. Production Architecture (HTTP Mode)

To handle Slack's strict 3-second timeout and prevent double-processing, the HTTP mode uses an asynchronous worker pattern.

### 3.1 Event Pipeline

1. **Ingress:** Slack sends a webhook to `/slack/events`.
2. **Verification:** `HTTPModeAdapter` verifies the `X-Slack-Signature` using HMAC-SHA256.
3. **Deduplication:** `FirestoreEventDedupStore` checks if the `event_id` has already been processed.
4. **Enqueue:** The event is enqueued to **Google Cloud Tasks**.
5. **Response:** The adapter immediately returns `200 OK` to Slack.
6. **Worker:** Cloud Tasks triggers the `/worker` endpoint, which executes the `ConversationHandler`.

### 3.2 Blueprint Pattern

The Slack adapter implements the **Blueprint Pattern** (Quart), allowing it to share port 8080 with other services (OAuth, Telegram, User Cabinet).

- **Prefix:** All Slack routes are prefixed with `/slack`.

---

## 4. Security & Authorization

### 4.1 Signature Verification

Every HTTP request is validated using the `SLACK_SIGNING_SECRET`.

- **Timestamp Check:** Prevents replay attacks by rejecting requests older than 5 minutes.
- **HMAC Validation:** Ensures the request originated from Slack.

### 4.2 IAM Integration

Every event (message or mention) triggers an `iam_service.authorize("slack", slack_user_id)` call.

- **Unauthorized Users:** Receive a registration link to the Web UI.
- **Authorized Users:** Proceed to the multi-agent reasoning loop.

---

## 5. Code References

- `src/composition/slack_adapter_factory.py`: Factory for creating the appropriate adapter (lives in `composition/` — creates `ConversationHandler` and injects it as a port).
- `src/adapters/slack/http_adapter.py`: Production webhook implementation.
- `src/adapters/slack/socket_adapter.py`: Development WebSocket implementation.
- `src/adapters/slack/response_channel.py`: Slack-specific message formatting.
- `src/adapters/gcp_task_queue.py`: Integration with Cloud Tasks.

---

## 6. Status & Roadmap

**Status:** ✅ Production Ready

### Planned Enhancements

- **Interactive Components:** Support for Slack buttons, menus, and modals.
- **App Home:** Implement a personalized dashboard within the Slack app.
- **Slash Commands:** Add native commands for quick actions (e.g., `/remember`, `/search`).

---

**Last Updated:** 2026-02-21
**Status:** ✅ Complete  
**Phase:** Documentation Audit Phase 3.6
