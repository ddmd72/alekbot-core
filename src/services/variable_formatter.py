"""
Variable formatter service for prompt variable injection.

Formats different data types (conversations, facts, anchors) into various
formats (XML, JSON, plain text) according to template specifications.

Part of hexagonal architecture:
- Service layer (application logic)
- No dependencies on infrastructure
- Pure transformation logic

Session: 26 (Variable Formatting System)
"""

import json
from typing import List, Dict, Any, Union
from datetime import datetime
import html


class VariableFormatter:
    """
    Formats prompt variables into specified formats.

    Supports formats:
    - xml: Structured XML for Claude
    - json: Standard JSON
    - plain: Plain text (legacy fallback)
    """

    def format(
        self,
        variable_name: str,
        data: Any,
        format_type: str = "plain"
    ) -> str:
        """
        Format variable according to specified format.

        Args:
            variable_name: Variable identifier (e.g., "CONVERSATION_INPUT")
            data: Data to format (can be dict, list, str, etc.)
            format_type: Target format ("xml", "json", "plain")

        Returns:
            Formatted string ready for injection
        """
        if format_type == "xml":
            return self._format_xml(variable_name, data)
        elif format_type == "json":
            return self._format_json(data)
        elif format_type == "plain":
            return self._format_plain(data)
        else:
            raise ValueError(f"Unknown format type: {format_type}")

    def _format_xml(self, variable_name: str, data: Any) -> str:
        """Route to specific XML formatter based on variable name."""
        if variable_name == "CONVERSATION_INPUT":
            return self._format_conversation_xml(data)
        elif variable_name == "BIOGRAPHICAL_CONTEXT":
            return self._format_biographical_xml(data)
        else:
            # Generic XML formatting
            return f"<{variable_name.lower()}>\n{self._escape_xml(str(data))}\n</{variable_name.lower()}>"

    def _format_conversation_xml(self, data: Union[str, List[Dict]]) -> str:
        """
        Format conversation as XML.

        Input can be:
        - String (legacy format: "USER: text\\nASSISTANT: text")
        - List of dicts (structured: [{"role": "user", "content": "text", "timestamp": ...}])
        """
        if isinstance(data, str):
            # Parse legacy plain text format
            return self._parse_plain_conversation_to_xml(data)
        elif isinstance(data, list):
            # Already structured
            return self._format_structured_conversation_xml(data)
        else:
            return f"<conversation>\n{self._escape_xml(str(data))}\n</conversation>"

    def _parse_plain_conversation_to_xml(self, text: str) -> str:
        """
        Parse plain text conversation into XML.

        Input format:
            USER (timestamp): text
            ASSISTANT (timestamp): text

        Output format:
            <conversation>
              <turn role="user" timestamp="...">
                <text>...</text>
              </turn>
            </conversation>
        """
        lines = text.strip().split('\n')
        xml_parts = ['<conversation>']

        current_role = None
        current_timestamp = None
        current_text = []

        for line in lines:
            # Try to parse role line: "USER (timestamp):" or "ASSISTANT (timestamp):"
            if line.startswith('USER (') or line.startswith('ASSISTANT ('):
                # Save previous turn if exists
                if current_role and current_text:
                    text_content = '\n'.join(current_text).strip()
                    xml_parts.append(f'  <turn role="{current_role.lower()}" timestamp="{current_timestamp}">')
                    xml_parts.append(f'    <text>{self._escape_xml(text_content)}</text>')
                    xml_parts.append('  </turn>')
                    current_text = []

                # Parse new turn
                if line.startswith('USER ('):
                    current_role = 'user'
                    rest = line[5:]  # Remove "USER "
                else:
                    current_role = 'assistant'
                    rest = line[11:]  # Remove "ASSISTANT "

                # Extract timestamp (between parentheses)
                if rest.startswith('(') and ')' in rest:
                    timestamp_end = rest.index(')')
                    current_timestamp = rest[1:timestamp_end]
                    # Text starts after "): "
                    if rest[timestamp_end:].startswith('): '):
                        text_start = rest[timestamp_end + 3:]
                        if text_start:
                            current_text.append(text_start)
                else:
                    current_timestamp = ""
                    current_text.append(rest.lstrip(':').strip())
            else:
                # Continuation of current turn
                if current_role:
                    current_text.append(line)

        # Save last turn
        if current_role and current_text:
            text_content = '\n'.join(current_text).strip()
            xml_parts.append(f'  <turn role="{current_role.lower()}" timestamp="{current_timestamp}">')
            xml_parts.append(f'    <text>{self._escape_xml(text_content)}</text>')
            xml_parts.append('  </turn>')

        xml_parts.append('</conversation>')
        return '\n'.join(xml_parts)

    def _format_structured_conversation_xml(self, turns: List[Dict]) -> str:
        """Format structured conversation data as XML."""
        xml_parts = ['<conversation>']

        for turn in turns:
            role = turn.get('role', 'unknown')
            timestamp = turn.get('timestamp', '')
            content = turn.get('content') or turn.get('text', '')

            xml_parts.append(f'  <turn role="{role}" timestamp="{timestamp}">')
            xml_parts.append(f'    <text>{self._escape_xml(content)}</text>')
            xml_parts.append('  </turn>')

        xml_parts.append('</conversation>')
        return '\n'.join(xml_parts)

    def _format_biographical_xml(self, data: Union[str, List, Dict]) -> str:
        """
        Format biographical context as XML.

        Input can be:
        - String (legacy format: bullet list)
        - List of strings (bullet list)
        - List of dicts (structured facts)
        - Dict (JSON biographical context)
        """
        if isinstance(data, str):
            # Parse bullet list
            return self._parse_bullet_list_to_xml(data)
        elif isinstance(data, list):
            if not data:
                return "<biographical_facts />"

            # Check if list of dicts (structured facts) or strings (bullet list)
            if isinstance(data[0], dict):
                return self._format_structured_facts_xml(data)
            else:
                # List of strings
                return self._parse_bullet_list_to_xml('\n'.join(str(item) for item in data))
        elif isinstance(data, dict):
            # JSON object
            return self._format_structured_facts_xml([data])
        else:
            return f"<biographical_facts>\n{self._escape_xml(str(data))}\n</biographical_facts>"

    def _parse_bullet_list_to_xml(self, text: str) -> str:
        """
        Parse bullet list into XML facts.

        Input format:
            - Fact one
            - Fact two

        Output format:
            <biographical_facts>
              <fact>Fact one</fact>
              <fact>Fact two</fact>
            </biographical_facts>
        """
        lines = text.strip().split('\n')
        xml_parts = ['<biographical_facts>']

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Remove bullet point (-, *, •, etc.)
            if line.startswith('-') or line.startswith('*') or line.startswith('•'):
                line = line[1:].strip()

            if line:
                xml_parts.append(f'  <fact>{self._escape_xml(line)}</fact>')

        xml_parts.append('</biographical_facts>')
        return '\n'.join(xml_parts)

    def _format_structured_facts_xml(self, facts: List[Dict]) -> str:
        """Format structured facts as XML with metadata."""
        xml_parts = ['<biographical_facts>']

        for fact in facts:
            if isinstance(fact, str):
                xml_parts.append(f'  <fact>{self._escape_xml(fact)}</fact>')
            elif isinstance(fact, dict):
                # Extract common fields
                content = fact.get('content') or fact.get('text', '')
                category = fact.get('category', '')
                tags = fact.get('tags', [])

                if category:
                    xml_parts.append(f'  <fact category="{category}">{self._escape_xml(content)}</fact>')
                elif tags:
                    tags_str = ', '.join(tags) if isinstance(tags, list) else tags
                    xml_parts.append(f'  <fact tags="{tags_str}">{self._escape_xml(content)}</fact>')
                else:
                    xml_parts.append(f'  <fact>{self._escape_xml(content)}</fact>')

        xml_parts.append('</biographical_facts>')
        return '\n'.join(xml_parts)

    def _format_json(self, data: Any) -> str:
        """Format as JSON (standard serialization)."""
        return json.dumps(data, indent=2, ensure_ascii=False)

    def _format_plain(self, data: Any) -> str:
        """Format as plain text (fallback)."""
        if isinstance(data, str):
            return data
        elif isinstance(data, (list, dict)):
            return json.dumps(data, indent=2, ensure_ascii=False)
        else:
            return str(data)

    def _escape_xml(self, text: str) -> str:
        """Escape XML special characters."""
        return html.escape(text, quote=False)
