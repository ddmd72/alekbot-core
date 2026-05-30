---
category: output_format
class: output_format
metadata:
  created_at: '2026-02-02'
  description: Strict JSON output for machine processing
  override_by:
  - AGENT
  use_case: Router agent, Consolidation agent - structured data output
  validation:
    action_taken: passed
    adapter: noop
    context: token_creation
    patterns_detected: []
    risk_level: SAFE
    risk_score: 0.0
    zone: trusted
token_id: OUTPUT_FORMAT_JSON
---
    output_schema {
        contract: "RAW JSON only — first char '{', last char '}'. No prose, no code blocks."

        schema: {
            "type": "object",
            "required": ["full_response", "response_summary", "rich_content"],
            "properties": {
                "full_response":    { "type": "string" },
                "response_summary": { "type": "string", "maxLength": 300 },
                "rich_content":     { "type": ["object", "null"], "properties": {
                    "type":     { "enum": ["widget", "file", "table"] },
                    "data":     { "type": "object", "properties": {
                        "rows":    { "type": "array", "items": { "type": "object", "properties": { "cells": { "type": "array", "items": { "type": "string" } } } } },
                        "headers": { "type": "array", "items": { "type": "string" } },
                        "title":   { "type": "string" },
                        "footer":  { "type": "string" },
                        "html":    { "type": "string" },
                        "alt_text":{ "type": "string" }
                    }},
                    "fallback": { "type": "string" }
                }},
                "link_list": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["anchor", "title", "url"],
                        "properties": {
                            "anchor": { "type": "string" },
                            "title":  { "type": "string" },
                            "url":    { "type": "string" }
                        }
                    }
                }
            }
        }
    }

    field_guidelines {
        full_response: "Slack mrkdwn: *bold*, _italic_, • for lists. No HTML, no **double-asterisk**, no # headers. Natural flow with line breaks. Use emojis."
        response_summary: "Max 300 chars, plain text. Preserve tone and emojis."
        rich_content: "null by default. Use when visual layout adds clear value."
        link_list: """
            Empty array [] by default.
            Populate ONLY when delegate_to_specialist returned links (e.g. Google Maps URLs, article URLs).
            Each item: anchor (numeric string matching a [N] citation in full_response), title (human label), url (exact URL from specialist).
            In full_response embed citations as [N] after the referenced name — e.g. "Bar Casa Vio [1] is located at...".
            The platform renderer converts [N] to a clickable link. Never show raw URLs in full_response — use [N] anchors only.
        """
    }

    rich_content_types {
        widget {
            when: "Weather, prices, schedules, rankings, comparisons — when visual card adds value over plain text."
            data: { html: "self-contained HTML", alt_text: "plain text description" }
            style: "Compact inline-CSS only. No <script>. Max 480px. iOS widget aesthetic — rounded corners 12-16px, dense layout, no chrome. 4+ items → CSS Grid 2-3 cols."
            fallback: "Plain text version of the visual."
        }

        table {
            when: "Comparative data: prices, forecasts, rankings, schedules — any 3+ items with shared attributes."
            data: {
                title: "Optional heading",
                headers: ["Col1", "Col2", "Col3"],
                rows: [
                    {"cells": ["val1", "val2", "val3"]},
                    {"cells": ["val4", "val5", "val6"]}
                ],
                footer: "Optional source/note"
            }
            rows_rule: "CRITICAL: rows is ALWAYS array of row-objects. Each object has exactly one key 'cells' — an array of strings. rows[i].cells.length == headers.length. NEVER a flat array of strings. NEVER duplicate 'rows' keys."
            fallback: "Plain text version of the table."
        }

        file {
            when: "User asks for a file that has no specialist agent for it."
            data: { filename: "name.ext", title: "Human-readable title", content: "Full file content" }
            content_format: ".html → full HTML | .xlsx → CSV string | .md → Markdown"
        }
    }

    examples {
        widget_example: '''
        {
            "full_response": "Weather in Berlin today:\n• ☀️ Day: +22°C, clear\n• 🌙 Night: +14°C\n• 💨 Wind: 3 m/s",
            "response_summary": "☀️ Berlin: +22°C day, +14°C night, clear",
            "rich_content": {
                "type": "widget",
                "data": {
                    "html": "<!-- compact self-contained HTML, inline CSS, iOS widget style -->",
                    "alt_text": "Berlin: +22°C day, clear sky"
                },
                "fallback": "Berlin: +22°C day, +14°C night, clear, wind 3 m/s"
            }
        }
        '''

        table_example: '''
        {
            "full_response": "Here's a comparison of the plans:\n\n• *Basic* — 10 GB, no support, $9/mo\n• *Pro* — 100 GB, email support, $29/mo\n• *Enterprise* — unlimited, 24/7 support, $99/mo\n\n_Pro is the best value for most users_ 👌",
            "response_summary": "Plans: Basic $9, Pro $29, Enterprise $99/mo",
            "rich_content": {
                "type": "table",
                "data": {
                    "title": "Plan Comparison",
                    "headers": ["Plan", "Storage", "Support", "Price"],
                    "rows": [
                        {"cells": ["Basic", "10 GB", "—", "$9/mo"]},
                        {"cells": ["Pro", "100 GB", "Email", "$29/mo"]},
                        {"cells": ["Enterprise", "Unlimited", "24/7", "$99/mo"]}
                    ],
                    "footer": "_Prices as of March 2026_"
                },
                "fallback": "Basic: 10GB $9/mo | Pro: 100GB $29/mo | Enterprise: Unlimited $99/mo"
            }
        }
        '''
    }
