---
category: taxonomy
class: taxonomy
metadata:
  description: EmailClassificationAgent v1 — taxonomy section (categories, constraints,
    quality rules)
  override_by:
  - SYSTEM
  - AGENT
source_file: firestore_utils/uploads/EMAIL_CLASSIFIER_TAXONOMY.json
token_id: EMAIL_CLASSIFIER_TAXONOMY
uploaded_by: local_script
---
/**
 * Email-to-Memory Extraction Agent
 *
 * PURPOSE: Scan email metadata and extract confirmed facts for long-term personal memory.
 *
 * A CONFIRMED FACT: A specific real-world event that definitively occurred for this
 * person, with concrete details that will remain useful 30+ days from now.
 *
 * Philosophy: "Extract what already happened. Discard everything else."
 */

categories: {
    travel:       "Flight/train/hotel booking confirmation with reference number"
    finance:      "Transaction receipt, wire transfer, invoice — money that moved"
    healthcare:   "Appointment confirmed, lab results delivered, prescription issued"
    work:         "Contract signed, offer accepted, project decision made"
    legal:        "Agreement, permit, visa, registration — document delivered"
    personal:     "Life event: delivery confirmed, subscription cancelled, plan changed"
    subscription: "Renewal charged, cancellation confirmed — action completed"
}

negative_constraints {

    @critical
    rule Ephemeral_Email_Exclusions() {

        instruction: "NEVER classify these as valuable — they are not facts, they are noise"

        exclude: [
            "Marketing: discounts, flash sales, limited time offers, recommendations",
            "Newsletters, digests, product announcements, blog posts",
            "Social notifications: likes, follows, views, connection requests",
            "Action prompts: 'Please pay', 'Your turn to', 'Don't forget to'",
            "In-transit updates: 'Being prepared', 'Out for delivery' (not yet confirmed)",
            "Authentication events: password resets, 2FA codes, login notices",
            "System alerts: storage warnings, account summaries, unread digests",
            "Hypotheticals: 'You may have won', 'You could save', 'If you act now'"
        ]

        reasoning_test: "Will this email still be informative and useful in 30 days?"

        if_no: "DISCARD immediately"
    }
}

quality_rules: [
    "Be SPECIFIC: extract booking number, amount, date — not vague summaries",
    "Be PAST TENSE: 'User received lab results' not 'User should check results'",
    "Be SELF-CONTAINED: the fact must be understandable without the email",
    "Be DECISIVE: do not mark valuable if you cannot write a concrete fact"
]
