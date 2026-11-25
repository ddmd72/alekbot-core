---
category: mcp_memory_inbox
class: mcp_texts
metadata:
  description: Memory inbox MCP resource (/memory-inbox-mcp) — the result lines of
    submit_user_information by key (received, cut, over the day's cap, nothing usable,
    store failed, the neutral refusal of a grant no longer allowed), formatted by code with counts only and never the submitted text
    (MCP_MEMORY_SUBMISSIONS_RFC §5.3). A draft, tuned live; no profile references it.
  override_by:
  - SYSTEM
  - AGENT
  validation:
    action_taken: passed
    adapter: noop
    context: token_creation
    patterns_detected: []
    risk_level: SAFE
    risk_score: 0
    zone: trusted
source_file: firestore_utils/uploads/MCP_MEMORY_INBOX_RESULT_TEXTS.json
token_id: MCP_MEMORY_INBOX_RESULT_TEXTS
uploaded_by: local_script
---
### RECEIVED
Items received for the user's memory inbox: {received}. Nothing is in memory and nothing is searchable yet: the items are filtered first, and only records the user then confirms reach memory. The received items are done; do not send them again.

### CUT
Items or fields cut: {cut}. Items past the per-call limit were not received, and a text over its length limit was kept only in part.

### OVER_DAY
Items not accepted today: {over_day}. The user's inbox has reached its limit for the day, so they were not stored. This is not an error.

### NOTHING_USABLE
Nothing was received: no item carried any information. Send each thing about the user as an item whose information field holds it as text.

### STORE_FAILED
Nothing was received: the inbox could not store the items because of a problem on the server. Try again later.

### REFUSED
Nothing was received: this connector cannot send to the user's memory inbox now. Do not retry; the user has to connect it again.
