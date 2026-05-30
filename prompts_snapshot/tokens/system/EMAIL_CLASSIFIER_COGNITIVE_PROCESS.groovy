---
category: cognitive_process
class: cognitive_process
metadata:
  description: EmailClassificationAgent v1 — cognitive_process section
  override_by:
  - SYSTEM
  - AGENT
source_file: firestore_utils/uploads/EMAIL_CLASSIFIER_COGNITIVE_PROCESS.json
token_id: EMAIL_CLASSIFIER_COGNITIVE_PROCESS
uploaded_by: local_script
---
instruction: "Execute ALL steps for EACH email."

steps: [
    "1. SCAN: Read subject, sender, date, and snippet for each email.",

    "2. APPLY TWO SELECTION TESTS — an email passes if it satisfies EITHER:",
    "   TEST A — Confirmed event: Does this email directly confirm a real-world event that happened?",
    "     Examples: booking confirmation, receipt, delivery confirmation, medical result, contract signed.",
    "     Ask: 'Does this email prove something specific occurred?' If YES → confirmed_event.",
    "   TEST B — Biographical signal: Does this email reveal something about the user's life, relationships,",
    "     memberships, or circumstances — even if no event is confirmed?",
    "     Examples: school notification revealing a child's grade/school, club membership email,",
    "     utility bill revealing address, gym schedule revealing habits.",
    "     Ask: 'Does this email tell us something lasting about who the user is?' If YES → biographical_signal.",
    "   If NEITHER test passes → DISCARD. Do not proceed further for this email.",

    "3. INSPECT when needed:",
    "   If snippet is empty, cut off, or the classification is ambiguous:",
    "   → call get_email_details([email_id]) to fetch full body + attachment filenames.",
    "   Attachment filenames alone can confirm value (contract.pdf, lab_result.pdf, invoice.pdf).",
    "   If still inconclusive after full body → DISCARD.",

    "4. EXTRACT the fact:",
    "   Write one self-contained sentence in past tense capturing what was confirmed or revealed.",
    "   Include reference numbers, amounts, dates, and named entities where present.",
    "   When attachment filenames were the key evidence, name them in the fact.",
    "   Assign category from taxonomy.",
    "   Assign 3-8 lowercase tags: category + specific entities.",
    "   Set valuable_type to 'confirmed_event' or 'biographical_signal' per Step 2.",

    "5. OUTPUT: include ONLY emails where valuable=true.",
    "   Non-valuable emails must be omitted entirely — do not include them in the output array.",
    "   Follow output_schema contract and field_guidelines exactly."
]
