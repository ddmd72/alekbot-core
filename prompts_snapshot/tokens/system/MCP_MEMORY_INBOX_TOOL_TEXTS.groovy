---
category: mcp_memory_inbox
class: mcp_texts
metadata:
  description: 'Memory inbox MCP resource (/memory-inbox-mcp) — the server instructions,
    the description of submit_user_information, of its items parameter and of each
    item field (key tool.items.field), served per tools/list; written for an outside
    assistant: what the memory it feeds is, what happens to an item (a daily filter,
    the user checks each record, a final pass decides), the no-retell loop guard, then
    the shape (MCP_MEMORY_SUBMISSIONS_RFC §5.3). A draft, tuned live; no profile references
    it.'
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
source_file: firestore_utils/uploads/MCP_MEMORY_INBOX_TOOL_TEXTS.json
token_id: MCP_MEMORY_INBOX_TOOL_TEXTS
uploaded_by: local_script
---
### instructions
This connector passes what you learn about the user in your conversations to the user's long-term memory, which their own assistant keeps and answers from. It only sends: it cannot read that memory, and nothing you send comes back to you. Use submit_user_information when you learn something about the user's life that is worth knowing beyond this conversation: from what they tell or show you, or from what you conclude from it. A task the user asks you to do, and the code or text they work on, is not about their life unless it shows something lasting about them.

### submit_user_information
What it feeds: the user's own assistant keeps a long-term memory of the user's life (the user, the people around them, what they do, plan, prefer and decide) and answers the user from it later. This tool hands that memory what you learn about the user in this conversation, which it has no other way to learn.

What happens to what you send: nothing becomes memory on arrival and nothing is searchable. A separate pass compares every item with what memory already holds and drops what it already knows, what is trivial and what is not about the user. Every record that remains is shown to the user, who checks each one, one by one, for truth; only what the user confirms reaches a final pass, which decides what memory keeps. Doubt is handled there: send what you learned, even when you are unsure it matters, and leave the judgment to the filter and the user.

What not to send: send only what comes from this conversation: what the user said or showed, or what you concluded from it (basis inferred). Never send back anything the memory read tool (get_user_context) returned, as it was or reworded, also when the user only agrees with it: memory already holds it, and sending it back closes a loop in which memory confirms itself and fills with duplicates. Only what the user adds to such a record or changes in it is new. Something you concluded with the help of what that tool returned is your own inference, not the record itself: send it with basis inferred.

How to shape it: one item is one thing about the user, in plain sentences that make sense without this conversation, naming the people and things they concern. A message that says several things becomes several items. Give times in the user's own words: precision is not required, and naming your uncertainty is better than inventing a date. When the user corrects or changes something you already sent, send a new item that says what it corrects; what was sent cannot be withdrawn.

### submit_user_information.items
The things to pass on, one item per thing about the user: each an object with the information itself and, when you know them, how you know it and when. A call takes a limited number of items and the user's inbox a limited number per day; the answer says how many were received and whether any were cut or not accepted.

### submit_user_information.items.information
One thing about the user, in plain sentences that stand on their own: whom or what it concerns and what is the case, condensed from what the user said or showed, or the conclusion you drew from it, never embellished. Required: an item without it is not received.

### submit_user_information.items.basis
How you know it, which tells the user and the later pass how much weight it carries. said: the user told you directly. document: it comes from a document, file or image the user showed you. inferred: you worked it out yourself, including anything you concluded with the help of what the memory read tool returned. Left empty or given any other value, it counts as inferred, the least trusted.

### submit_user_information.items.said_when
When the user said it, only when that was not today: a conversation can run over several days, and the date of a statement is what ranks it against another that says something different. In words; approximate is fine. Empty means the user said it today.

### submit_user_information.items.happens_when
When the thing itself happened, happens or will happen, in the words the user used. Approximate is fine: name your uncertainty rather than invent a precise date. Empty means the user gave no time.
