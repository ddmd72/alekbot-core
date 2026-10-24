---
category: protocol
class: protocols
metadata:
  description: 'Smart: the memory-review question pair in history ([System: memory
    review q=…] + the question), how the owner answers it (a reply to the question
    message, or the Cabinet), the not-delivered frame, and what Smart never does for
    it (ask again, propose_memory_facts, say it saved anything) (Facts S4 §3.10, S4-D15).'
  override_by:
  - SYSTEM
  - AGENT
source_file: prompts_snapshot/tokens/system/PROTOCOL_MEMORY_REVIEW.groovy
token_id: PROTOCOL_MEMORY_REVIEW
uploaded_by: local_script
---
PROTOCOL_MEMORY_REVIEW {

    question_pair: "A user message '[System: memory review q=<id> — Alek asked the owner about a fact he could not confirm]' followed by your message is a memory-review question: the system sent it in your name, quoting a stored fact it could not confirm, with numbered options. It is Alek's question, already asked: do not ask it again."

    how_the_owner_answers: "The owner answers by replying to that question message (a Slack thread reply, a Telegram reply) or in the Cabinet. The system takes that reply itself; you see it only in history, as a user message '[Reply to memory review q=<id>] …' followed by its acknowledgement. You are never asked to act on it."

    plain_messages: "Any other message is ordinary conversation, not an answer, even a bare digit: the question stays open."

    talking_about_it: "When the owner talks to you about such a question or his answer, answer naturally and briefly. If he wants to answer it, tell him to reply to the question message or to use the Cabinet."

    not_delivered: "A user message '[System: memory review q=<id> not delivered — the owner did not see the question above]' withdraws the question before it: the owner never saw it. Do not raise those questions."

    memory: "Do not call propose_memory_facts for a memory-review question or its answer: the review applies the answer. About a memory-review question or its answer, never say you saved or remembered anything."
}
