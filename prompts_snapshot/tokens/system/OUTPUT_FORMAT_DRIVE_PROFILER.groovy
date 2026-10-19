---
category: output_format
class: output_format
metadata:
  description: DriveProfilerAgent — output_format section (mirrors _RESPONSE_SCHEMA)
  override_by:
  - SYSTEM
  - AGENT
  source: 'v2 draft 2026-10-13: Search v2 (USER_DRIVE_INDEXING_RFC §25.4), not uploaded'
source_file: firestore_utils/uploads/OUTPUT_FORMAT_DRIVE_PROFILER.json
token_id: OUTPUT_FORMAT_DRIVE_PROFILER
uploaded_by: local_script
---
format: "One JSON object and nothing else: no markdown fences, no text before or after it."

shape: '{"title": string, "what_it_is": string, "language": string, "doc_type": string, "life_domains": [string], "dates": [{"label": string, "date": "YYYY-MM-DD", "printed": string}], "entities": [{"name": string, "type": string, "printed": string, "forms": [string]}], "key_values": [{"label": string, "printed": string, "canonical": string}], "topics": [string], "topics_native": [string], "topic_words": [string], "label_words": [string], "domain_words": [string], "validity_words": [string]}'

fields: [
    "Every field is required: an empty string or an empty list when there is nothing.",
    "doc_type: one of receipt, invoice, bill, bank_statement, payslip, tax_document, contract, insurance_policy, warranty, ticket, booking, certificate, id_document, medical_record, prescription, fine, letter, form, manual, book, article, report, plan, notes, recipe, newsletter, web_page, spreadsheet, data, presentation, other.",
    "life_domains: one to three of finance, health, travel, home, vehicle, work, education, legal_admin, shopping, leisure, family, tech, news_reference, other.",
    "dates.label: one of issued, event, valid_from, valid_until, due, period_end, other; dates.date: YYYY-MM-DD; dates.printed: the date exactly as the document prints it.",
    "entities.type: one of place, organization, person, event, product, work, other.",
    "key_values.label: one of order_number, ticket_number, booking_reference, invoice_number, receipt_number, account_number, iban, card_number_masked, policy_number, contract_number, customer_number, reference, tax_id, document_number, personal_id_number, phone, amount_total, amount_due, price, other_id.",
    "key_values: an empty list when the task says this kind of file has none.",
]
