import json
from typing import Optional

from ..ports.llm_port import LLMPort, LLMRequest
from ..domain.llm import Message, MessagePart
from ..utils.logger import logger


class HistorySummaryService:
    """
    Compresses conversation history entries via fast LLM call.

    Always uses Gemini structured output (response_schema).
    Provider is locked to Gemini at composition time (UserAgentFactory).
    Fail-fast: one attempt, WARNING on failure, returns None (caller uses full text).
    """

    def __init__(self, llm_port: LLMPort, model_name: str):
        self.provider = llm_port
        self.model_name = model_name

    async def summarize_model_response(self, response_text: str) -> Optional[str]:
        """
        Compress assistant response to ≤300 chars for session memory.

        Returns None on failure — caller falls back to storing full text.
        """
        request = LLMRequest(
            model_name=self.model_name,
            system_instruction=(
                "You compress assistant responses into ultra-compact session memory entries.\n\n"
                "Rules for the `summary` field:\n"
                "- Max 300 chars. Hard limit — never exceed.\n"
                "- Include: key entities, facts, decisions made.\n"
                "- Preserve the vibe (irony, skepticism, precision) — don't make it robotic.\n"
                "- Emojis: STRICTLY preserve or adapt emojis from the original. They carry emotional weight.\n"
                "- DO NOT interpret or expand upon the original text. Summarize only\n"
                "- Plain text only. No Markdown (*bold*, _italic_), no blockquotes.\n\n"
                "Examples:\n"
                "  [Irony/Tech]: XML as a delimiter — works for Claude, overkill for Gemini. Groovy is cleaner. 🧱\n"
                "  [Factual]: Dietary: purine diet, no anchovies/mussels, approved: venison, mackerel, salmon.\n"
                "  [Analytical]: Role Prompting: 4 components — Persona, Context, Task, Tone. Anti-role trick noted. 🎭"
            ),
            messages=[Message(role="user", parts=[MessagePart(text=response_text)])],
            temperature=0.0,
            response_mime_type="application/json",
            response_schema={
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"]
            }
        )
        try:
            result = await self.provider.generate_content(request=request)
            if result.text:
                parsed = json.loads(result.text)
                summary = parsed.get("summary", "").strip()
                logger.debug("⚡ [HistorySummaryService] Summary generated (%d chars)", len(summary))
                return summary or None
        except Exception as exc:
            logger.warning("⚡ [HistorySummaryService] Summary failed: %s", exc)
        return None
