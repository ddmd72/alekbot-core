"""
Groovy to Markdown Transformer for Claude.

Parses Groovy DSL prompts and converts them to Markdown format optimized for Claude.
Uses Lark for parsing.
"""

import os
from typing import Any, Dict, List, Optional
from lark import Lark, Transformer, v_args, Token
from ..utils.logger import logger

class GroovyToMarkdownTransformer(Transformer):
    """
    Transforms Groovy DSL parse tree into Markdown string.
    """
    
    def start(self, items):
        return "\n".join(items)
        
    def class_def(self, items):
        # items: ["class", NAME, "extends", NAME, "{", class_body, "}"]
        # Find the item that looks like body
        body = items[-1] if items else ""
        return f"# System Instructions\n\n{body}"

    def class_body(self, items):
        return "\n".join(items)

    def annotated_block(self, items):
        # items: [annotation, body] (punctuation removed)
        annotation = items[0]
        body = items[-1]
        name = annotation[1:] # remove @
        return f"## {name}\n{body}\n"

    def rule_def(self, items):
        # rule_def: (annotation)? "rule" NAME ("(" ")")? "{" body "}"
        
        annotation = ""
        name = ""
        body = items[-1] # Body is last item (punctuation removed)
        
        for item in items:
            if hasattr(item, 'type') and item.type == 'NAME':
                name = item.value
                break
            elif isinstance(item, str) and item.isidentifier() and item != "rule" and item != body and not item.startswith("@"):
                 name = item
                 break
                
        for item in items:
            if isinstance(item, str) and item.startswith("@"):
                annotation = f' ({item[1:]})'
                break
                    
        return f"### Rule: {name}{annotation}\n{body}\n"

    def structure(self, items):
        # structure: (annotation)? NAME ("(" ")")? "{" body "}"
        
        annotation = ""
        name = ""
        body = items[-1] # Body is last item
        
        for item in items:
            if hasattr(item, 'type') and item.type == 'NAME':
                name = item.value
                break
            elif isinstance(item, str) and item.isidentifier() and item != body and not item.startswith("@") and not item.startswith("<"):
                 name = item
                 break
        
        for item in items:
            if isinstance(item, str) and item.startswith("@"):
                annotation = f' ({item[1:]})'
                break
                    
        return f"## {name}{annotation}\n{body}\n"

    def body(self, items):
        return "\n".join(items)

    def property(self, items):
        # items: [NAME, ":", value]
        name = items[0]
        value = items[1]
        return f"- **{name}**: {value}"

    def value(self, items):
        return items[0]

    def list(self, items):
        # items: ["[", val, ",", val..., "]"]
        values = [item for item in items if item not in ["[", "]", ","]]
        # Format as bullet list if long, or inline
        return "\n  - " + "\n  - ".join(values)

    def map(self, items):
        # items: ["{", prop, ",", prop..., "}"]
        content = "\n  ".join([item for item in items if item not in ["{", "}", ","]])
        return f"\n  {content}"

    def MULTILINE_STRING(self, token):
        # Strip quotes ''' or """
        text = token.value
        if text.startswith("'''") or text.startswith('"""'):
            text = text[3:-3]
        return f"\n{text}\n"

    def annotation(self, items):
        return f"@{items[0]}"

    def comment(self, items):
        return "" 

    def method_call(self, items):
        return ""

    def STRING(self, token):
        # Remove quotes
        return token[1:-1]

    def NAME(self, token):
        return token.value


class GroovyToMarkdownConverter:
    """
    Facade for parsing and converting Groovy prompts.
    """
    
    def __init__(self):
        grammar_path = os.path.join(os.path.dirname(__file__), "groovy_grammar.lark")
        with open(grammar_path, "r") as f:
            self.grammar = f.read()
        
        self.parser = Lark(self.grammar, start="start", parser="lalr")
        self.transformer = GroovyToMarkdownTransformer()

    def convert(self, groovy_prompt: str) -> str:
        """
        Convert Groovy DSL string to Markdown.
        
        Args:
            groovy_prompt: The Groovy prompt string
            
        Returns:
            Markdown string
        """
        # Preprocessing: remove timestamp line if present
        lines = groovy_prompt.splitlines()
        timestamp = ""
        groovy_code = groovy_prompt
        
        if lines and lines[0].startswith("Current date and time is"):
            timestamp = lines[0]
            groovy_code = "\n".join(lines[1:])
            
        try:
            tree = self.parser.parse(groovy_code)
            md_content = self.transformer.transform(tree)
            
            # Add timestamp back
            if timestamp:
                md_content = f"{timestamp}\n\n{md_content}"
                
            return md_content
        except Exception as e:
            logger.error(f"Failed to convert Groovy to Markdown: {e}")
            # Fallback: return original prompt
            return groovy_prompt
