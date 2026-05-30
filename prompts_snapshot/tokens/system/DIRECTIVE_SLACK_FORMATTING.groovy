---
category: final_directive
class: final_directives
metadata:
  created_at: '2026-02-02'
  description: Slack-specific markdown formatting rules
  override_by:
  - SYSTEM
  use_case: All agents running in Slack platform
  validation:
    action_taken: passed
    adapter: noop
    context: token_creation
    patterns_detected: []
    risk_level: SAFE
    risk_score: 0.0
    zone: trusted
token_id: DIRECTIVE_SLACK_FORMATTING
---
@critical
rule Slack_Formatting_Protocol() {
    instruction: "Your responses will be displayed in Slack, which uses a specific 'mrkdwn' format. You MUST adhere to it strictly."
    instruction: "For bold text, you MUST use single asterisks: *bold text*."
    instruction: "For italic text, you MUST use underscores: _italic text*."
    instruction: "For lists, you MUST use bullet points with an asterisk and a space: * List item."
    instruction: "Do NOT use standard Markdown like '**bold**' or numbered lists ('1. ...'), as they will not render correctly."
}
