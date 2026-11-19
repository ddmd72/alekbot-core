"""
BiographicalFactsFormatter - Formats biographical facts for prompt injection.

Part of Prompt Design System v3 (RFC).

Sections follow the fact profile (FACT_PROFILE_RFC §6 row 3): one section per registry domain (`fact_domains.py`) of
each item's `primary_domain`, headed by its English label, biography first and the rest in registry order. Biography is
chronological (oldest first), the others newest first; the Router's prefetch (`semantic_lens`) stays a separate
"Query-Specific Context" section.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List

from src.domain.fact_domains import Domain, all_keys, is_known
from src.domain.fact_domains import label as domain_label

# The section of an item without a registry primary_domain: the derivation's own fallback for an unclassified subject.
_FALLBACK_DOMAIN = Domain.LIFESTYLE
_SEMANTIC = "semantic"
_SECTION_LOCALE = "en"


class BiographicalFactsFormatter:
    """Formats biographical facts into domain-grouped Markdown blocks.

    Expected input: List of fact dicts with keys:
    - text (str): Fact content
    - primary_domain (str): registry key of the fact's profile (absent or None: the lifestyle section)
    - tags (list[str]): "mindset" marks a behavioural anchor ([MINDSET] prefix, any domain)
    - created_at (ISO str): Timestamp for sorting
    """

    def format(self, facts: List[Dict]) -> str:
        """Format facts into domain-grouped Markdown sections.

        Args:
            facts: List of fact dictionaries from cache. Directives are separated upstream
                by PromptBuilder (own channel) and never reach this method.

        Returns:
            Markdown string ready for prompt injection.
        """
        if not facts:
            return ""

        grouped = self._group_by_domain(facts)
        order = [Domain.BIOGRAPHY] + [key for key in all_keys() if key != Domain.BIOGRAPHY]
        sections = [
            self._render_section(domain_label(key, _SECTION_LOCALE) or key, grouped[key]) for key in order if key in grouped
        ]

        if _SEMANTIC in grouped:
            sections.append(self._render_semantic_section(grouped[_SEMANTIC]))

        return "\n\n".join(section for section in sections if section)

    def format_directives(self, directives: List[Dict]) -> str:
        """Render a pre-separated directive list as a flat rulebook (newest first, no dates).

        The caller (PromptBuilder) owns the domain partition; this method only renders.
        Returns "" when empty — caller skips the block entirely.
        """
        items = [d for d in directives if isinstance(d, dict) and (d.get("text") or "").strip()]
        if not items:
            return ""

        items.sort(key=lambda d: d.get("created_at", ""), reverse=True)
        return "\n".join(f"- {d['text'].strip()}" for d in items)

    def _group_by_domain(self, facts: List[Dict]) -> Dict[str, List[Dict]]:
        """Group facts by registry primary_domain (none or unknown: lifestyle) and sort within each group.

        Sorting: biography oldest → newest (chronological); semantic keeps insertion order; all others newest → oldest.

        Returns:
            Dict mapping registry key (or "semantic") → sorted facts
        """
        by_domain: Dict[str, List[Dict]] = {}

        for fact in facts:
            if not isinstance(fact, dict):
                continue

            text = (fact.get("text") or "").strip()
            if not text:
                continue

            if "semantic_lens" in fact.get("tags", []):
                domain = _SEMANTIC
            else:
                domain = fact.get("primary_domain") or ""
                if not is_known(domain):
                    domain = _FALLBACK_DOMAIN

            by_domain.setdefault(domain, []).append(fact)

        for domain, domain_facts in by_domain.items():
            if domain == Domain.BIOGRAPHY:
                domain_facts.sort(key=lambda f: f.get("created_at", ""))
            elif domain != _SEMANTIC:
                domain_facts.sort(key=lambda f: f.get("created_at", ""), reverse=True)

        return by_domain

    def _render_section(self, label: str, facts: List[Dict]) -> str:
        """Render a domain section: its label, then one line per fact ([MINDSET] for the tag, recorded date)."""
        if not facts:
            return ""

        lines = [f"**{label}**"]

        for fact in facts:
            text = (fact.get("text") or "").strip()
            if not text:
                continue

            if "mindset" in fact.get("tags", []):
                text = f"[MINDSET] {text}"

            date_suffix = self._format_date(fact.get("created_at"))
            if date_suffix:
                text = f"{text} ({date_suffix})"

            lines.append(f"- {text}")

        return "\n".join(lines)

    def _render_semantic_section(self, facts: List[Dict]) -> str:
        """Render Query-Specific Context section (semantic facts).
        
        Session 2026-02-17: Kept as-is for now, optimization in future phase.
        
        Args:
            facts: Semantic facts from router enrichment
            
        Returns:
            Markdown formatted section
        """
        if not facts:
            return ""

        lines = ["**Query-Specific Context:**"]

        for fact in facts:
            text = (fact.get("text") or "").strip()
            if not text:
                continue

            lines.append(f"- {text}")

        if len(lines) == 1:
            return ""

        return "\n".join(lines)

    @staticmethod
    def _format_date(created_at) -> str:
        """Format datetime to human-readable date string.
        
        Args:
            created_at: ISO string or datetime object
            
        Returns:
            Formatted date like "Feb 17, 2026" or empty string
        """
        if not created_at:
            return ""

        if isinstance(created_at, datetime):
            return created_at.strftime("%b %d, %Y")

        if isinstance(created_at, str):
            try:
                normalized = created_at.replace("Z", "+00:00")
                parsed = datetime.fromisoformat(normalized)
                return parsed.strftime("%b %d, %Y")
            except ValueError:
                return ""

        return ""
