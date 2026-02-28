"""
EmailClassificationService — Gemini Flash batch classification of email metadata.
See docs/10_rfcs/GMAIL_EMAIL_INDEXING_RFC.md §4.

One LLM call per batch of up to 100 emails. No tool calling.
Classification is based on subject, sender, date, and snippet only.
Full content fetch (attachments) happens downstream in EmailIndexingService.
"""

import json
import re
from typing import List, Optional

from ..domain.email import EmailClassificationResult, EmailMetadata
from ..domain.llm import Message, MessagePart
from ..ports.llm_service import LLMRequest, LLMService
from ..utils.logger import logger

# ---------------------------------------------------------------------------
# Classification prompt (Groovy DSL, inline — not in Firestore because this is
# deterministic service logic, not a user-customizable agent persona token)
# ---------------------------------------------------------------------------
_CLASSIFICATION_PROMPT = """\
class EmailFactExtractor extends Agent {

    taxonomy {
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
                    "In-transit updates: 'Being prepared', 'Out for delivery' — not yet confirmed",
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
    }

    cognitive_process {
        instruction: "Execute ALL steps for EACH email. Reason before classifying."
        steps: [
            "1. SCAN: Read subject, sender, date, and snippet for each email.",
            "2. APPLY REASONING TEST:",
            "   Ask: 'Will this email still be informative and useful in 30 days?'",
            "   If NO → set valuable=false. Do not proceed further for this email.",
            "   If YES → continue to Step 3.",
            "3. EXTRACT the confirmed fact:",
            "   Write one self-contained sentence in past tense with all key specifics.",
            "   Include reference numbers, amounts, dates, and named entities where present.",
            "   Assign category from taxonomy.",
            "   Assign 3-8 lowercase tags: category + specific entities.",
            "4. OUTPUT a valid JSON array covering ALL emails — no exceptions:",
            "   [{email_id, valuable, category, fact, tags, reason}]",
            "   For valuable=false entries: category=null, fact=null, tags=[]"
        ]
    }
}
"""


class EmailClassificationService:
    """
    Classifies a batch of email metadata via a single Gemini Flash LLM call.

    Output schema per email:
      {email_id, valuable, category, fact, tags, reason}
    """

    def __init__(self, llm_service: LLMService, model_name: str = "gemini-2.0-flash"):
        self._llm = llm_service
        self._model_name = model_name
        logger.info(
            f"📧 EmailClassificationService initialized. Model: {model_name}"
        )

    async def classify_batch(
        self,
        emails: List[EmailMetadata],
        user_id: str,
    ) -> List[EmailClassificationResult]:
        """
        Classify up to 100 emails in a single LLM call.
        Returns results for all emails (including valuable=False ones).
        """
        if not emails:
            return []

        user_message = self._format_emails(emails)
        request = LLMRequest(
            model_name=self._model_name,
            system_instruction=_CLASSIFICATION_PROMPT,
            messages=[
                Message(role="user", parts=[MessagePart(text=user_message)])
            ],
            temperature=0.0,
            max_tokens=12000,  # 100 emails × ~120 tokens output each
            response_mime_type="application/json",
            disable_safety=True,
        )

        try:
            response = await self._llm.generate_content(request=request)
            raw = response.text or ""
            results = self._parse_response(raw, emails)
            logger.info(
                f"📊 classify_batch: {len(emails)} emails → "
                f"{sum(1 for r in results if r.valuable)} valuable"
            )
            return results
        except Exception as exc:
            logger.error(
                f"💥 EmailClassificationService.classify_batch failed: {exc}"
            )
            # Return all as not valuable on failure — safe default
            return [
                EmailClassificationResult(
                    email_id=e.email_id,
                    valuable=False,
                    category=None,
                    fact=None,
                    tags=[],
                    reason="classification_error",
                )
                for e in emails
            ]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_emails(emails: List[EmailMetadata]) -> str:
        """Format emails as a JSON array for the LLM."""
        items = []
        for e in emails:
            items.append({
                "email_id": e.email_id,
                "subject": e.subject,
                "from": e.from_address,
                "date": e.date.strftime("%Y-%m-%d") if e.date else "",
                "snippet": e.snippet[:300] if e.snippet else "",
            })
        return json.dumps(items, ensure_ascii=False)

    @staticmethod
    def _parse_response(
        raw: str, emails: List[EmailMetadata]
    ) -> List[EmailClassificationResult]:
        """
        Parse LLM JSON output into EmailClassificationResult list.
        Fills in missing email_ids as not-valuable to guarantee full coverage.
        """
        # Strip possible markdown code block wrapping
        json_match = re.search(r"\[.*\]", raw, re.DOTALL)
        if not json_match:
            logger.warning(
                f"⚠️ EmailClassificationService: no JSON array in response: {raw[:200]}"
            )
            return [
                EmailClassificationResult(
                    email_id=e.email_id,
                    valuable=False,
                    category=None,
                    fact=None,
                    tags=[],
                    reason="parse_error",
                )
                for e in emails
            ]

        try:
            items = json.loads(json_match.group(0))
        except json.JSONDecodeError as exc:
            logger.warning(f"⚠️ EmailClassificationService: JSON parse error: {exc}")
            return [
                EmailClassificationResult(
                    email_id=e.email_id,
                    valuable=False,
                    category=None,
                    fact=None,
                    tags=[],
                    reason="parse_error",
                )
                for e in emails
            ]

        # Index by email_id for O(1) lookup
        expected_ids = {e.email_id for e in emails}
        results_by_id: dict = {}

        for item in items:
            email_id = item.get("email_id", "")
            if email_id not in expected_ids:
                continue
            results_by_id[email_id] = EmailClassificationResult(
                email_id=email_id,
                valuable=bool(item.get("valuable", False)),
                category=item.get("category") or None,
                fact=item.get("fact") or None,
                tags=[t.lower() for t in (item.get("tags") or [])],
                reason=item.get("reason", ""),
            )

        # Fill missing emails as not-valuable
        results = []
        for e in emails:
            if e.email_id in results_by_id:
                results.append(results_by_id[e.email_id])
            else:
                results.append(
                    EmailClassificationResult(
                        email_id=e.email_id,
                        valuable=False,
                        category=None,
                        fact=None,
                        tags=[],
                        reason="missing_from_response",
                    )
                )

        return results
