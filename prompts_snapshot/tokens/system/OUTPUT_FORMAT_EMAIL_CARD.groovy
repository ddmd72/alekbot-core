---
category: output_format
class: output_format
metadata:
  description: 'EmailCardAgent: the card answer format of one mail (email index v2)'
  override_by:
  - SYSTEM
  - AGENT
  source: 'lean card, probe 2026-11-13, not uploaded'
token_id: OUTPUT_FORMAT_EMAIL_CARD
---
format: "One JSON object and nothing else: no markdown fences, no text before or after it."

shape: '{"language": string, "doc_type": string, "life_domains": [string], "title": string, "what_it_is": string, "people": [{"printed": string, "entry_id": string}], "about": string, "dates": [{"label": string, "date": "YYYY-MM-DD", "date_end": "YYYY-MM-DD or empty", "printed": string}], "entities": [{"name": string, "type": string, "printed": string, "forms": [string]}], "key_values": [{"label": string, "printed": string, "canonical": string}], "topics": [string], "topics_native": [string], "topic_words": [string], "substance": string}'

fields: [
    "Every field is required: an empty string or an empty list when there is nothing.",
    "doc_type: one of {DOC_TYPES}.",
    "life_domains: one to three of {LIFE_DOMAINS}.",
    "people.entry_id: an entry id exactly as the people list prints it, or an empty string.",
    "about: one of {ABOUT_VALUES}.",
    "dates.label: one of {DATE_LABELS}; dates.date and dates.date_end: YYYY-MM-DD, date_end empty for a single day; dates.printed: the date or span exactly as printed.",
    "entities.type: one of {ENTITY_TYPES}.",
    "key_values.label: one of {VALUE_LABELS}.",
    "substance: one of {SUBSTANCE_VALUES}.",
    "Code replaces or drops a value outside its list.",
]
