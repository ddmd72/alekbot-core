"""
BiographicalFactsFormatter - Formats biographical facts for prompt injection.

Part of Prompt Design System v3 (RFC).

Session 2026-02-17: Domain-based formatting refactor
- Removed hashtag noise (except [MINDSET] prefix)
- Grouped by FactDomain enum (biographical, health, preference, etc.)
- Dual sorting: biographical (oldest→newest), others (newest→oldest)
- Semantic facts (Query-Specific Context) kept as separate section
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List


class BiographicalFactsFormatter:
    """Formats biographical facts into domain-grouped Markdown blocks.

    Session 2026-02-17 Refactor:
    - Groups facts by domain (biographical, health, preference, etc.)
    - Removes hashtag noise (except [MINDSET] prefix for preference+mindset)
    - Dual sorting: biographical ascending (chronological), others descending (recent first)
    - Preserves semantic facts as separate "Query-Specific Context" section
    
    Expected input: List of fact dicts with keys:
    - text (str): Fact content
    - domain (str): FactDomain enum value (biographical, health, etc.)
    - tags (list[str]): Tags including "mindset" for behavioral anchors
    - created_at (ISO str): Timestamp for sorting
    """

    HEADER = "// Top biographical records. Use memory_search for more details."
    
    # Domain labels for section headers (human-readable, LLM-friendly)
    # Session 2026-02-17: Minimalistic - semantic key is sufficient for LLM
    DOMAIN_LABELS = {
        "biographical": "Biographical",
        "possession": "Possession",
        "health": "Health",
        "medical_records": "Medical Records",
        "location": "Location",
        "work": "Work",
        "network": "Network",
        "preference": "Preference",
        "skill": "Skill",
        "project": "Project",
        "finance": "Finance",
        "education": "Education",
        "legal": "Legal",
        "entertainment": "Entertainment",
        "communication": "Communication"
    }

    def format(self, facts: List[Dict]) -> str:
        """Format facts into domain-grouped Markdown sections.

        Args:
            facts: List of fact dictionaries from cache.

        Returns:
            Markdown string ready for prompt injection.
        """
        if not facts:
            return ""

        # Group by domain (biographical, health, etc.) and separate semantic
        grouped = self._group_by_domain(facts)
        sections = []

        if self.HEADER:
            sections.append(self.HEADER)

        # Render domain sections (biographical first, then alphabetical)
        domain_order = ["biographical"] + sorted([d for d in grouped.keys() if d not in ["biographical", "semantic"]])
        
        for domain in domain_order:
            if domain in grouped:
                label = self.DOMAIN_LABELS.get(domain, domain.replace("_", " ").title())
                sections.append(self._render_section(label, grouped[domain], domain))

        # Render semantic facts separately (Query-Specific Context)
        if "semantic" in grouped:
            sections.append(self._render_semantic_section(grouped["semantic"]))

        return "\n\n".join(section for section in sections if section)

    def _group_by_domain(self, facts: List[Dict]) -> Dict[str, List[Dict]]:
        """Group facts by domain and sort within each domain.
        
        Sorting logic (Session 2026-02-17):
        - biographical: oldest → newest (chronological order)
        - semantic: keep as-is (no sorting, handled separately)
        - all others: newest → oldest (recent facts first)
        
        Args:
            facts: List of fact dicts
            
        Returns:
            Dict mapping domain → sorted facts
        """
        by_domain = {}
        
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            
            text = (fact.get("text") or "").strip()
            if not text:
                continue
            
            # Check for semantic_lens tag (Query-Specific Context)
            tags = fact.get("tags", [])
            if "semantic_lens" in tags:
                domain = "semantic"
            else:
                domain = fact.get("domain", "unknown")
            
            by_domain.setdefault(domain, []).append(fact)
        
        # Sort within domains
        for domain, domain_facts in by_domain.items():
            if domain == "biographical":
                # Chronological: oldest → newest
                domain_facts.sort(key=lambda f: f.get("created_at", ""))
            elif domain != "semantic":
                # Recent first: newest → oldest
                domain_facts.sort(key=lambda f: f.get("created_at", ""), reverse=True)
            # semantic: no sorting (keep insertion order)
        
        return by_domain

    def _render_section(self, label: str, facts: List[Dict], domain: str) -> str:
        """Render a domain section with facts.
        
        Args:
            label: Human-readable section label (e.g., "Biographical")
            facts: List of facts in this domain
            domain: Domain key (for mindset prefix logic)
            
        Returns:
            Markdown formatted section
        """
        if not facts:
            return ""

        lines = [f"**{label}**"]
        
        for fact in facts:
            text = (fact.get("text") or "").strip()
            if not text:
                continue

            # Session 2026-02-17: [MINDSET] prefix for preference domain + mindset tag
            tags = fact.get("tags", [])
            if domain == "preference" and "mindset" in tags:
                text = f"[MINDSET] {text}"

            # Add date suffix
            created_at = fact.get("created_at")
            date_suffix = self._format_date(created_at)
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
