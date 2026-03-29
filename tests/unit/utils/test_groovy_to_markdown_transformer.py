"""
Unit tests for GroovyToMarkdownTransformer and GroovyToMarkdownConverter.

Coverage:
  GroovyToMarkdownConverter.convert()
    - class definition with extends → "# System Instructions" header
    - annotated_block → "## @Name" section
    - rule_def without annotation → "### Rule: name"
    - rule_def with annotation → "### Rule: name (tag)"
    - structure without annotation → "## name" section
    - structure with annotation → "## name (tag)"
    - property with string value → "- **key**: value"
    - property with list value → bullet list
    - property with multiline string → stripped quotes + newlines
    - single-line comment → omitted from output
    - method_call → omitted from output
    - timestamp prefix stripped and re-prepended
    - parse error → returns original prompt unchanged
    - empty class body → renders header only
"""

import pytest

from src.utils.groovy_to_markdown_transformer import GroovyToMarkdownConverter


@pytest.fixture(scope="module")
def converter():
    return GroovyToMarkdownConverter()


# ---------------------------------------------------------------------------
# class_def
# ---------------------------------------------------------------------------

class TestClassDef:

    def test_class_with_extends_produces_system_instructions_header(self, converter):
        groovy = 'class MyAgent extends ClaudePersona { }'
        result = converter.convert(groovy)
        assert "# System Instructions" in result

    def test_class_body_content_rendered_inside(self, converter):
        groovy = '''class MyAgent extends ClaudePersona {
    identity {
        role: "assistant"
    }
}'''
        result = converter.convert(groovy)
        assert "# System Instructions" in result
        assert "identity" in result
        assert "role" in result


# ---------------------------------------------------------------------------
# annotated_block
# ---------------------------------------------------------------------------

class TestAnnotatedBlock:

    def test_annotated_block_renders_as_h2(self, converter):
        groovy = '@critical { guidance: "must follow" }'
        result = converter.convert(groovy)
        assert "## critical" in result

    def test_annotated_block_body_rendered(self, converter):
        groovy = '@important { guidance: "be helpful" }'
        result = converter.convert(groovy)
        assert "guidance" in result
        assert "be helpful" in result


# ---------------------------------------------------------------------------
# rule_def
# ---------------------------------------------------------------------------

class TestRuleDef:

    def test_rule_without_annotation(self, converter):
        groovy = 'rule MyRule { description: "do this" }'
        result = converter.convert(groovy)
        assert "### Rule: MyRule" in result
        assert "description" in result

    def test_rule_with_annotation_includes_tag(self, converter):
        groovy = '@critical rule MyRule { description: "always" }'
        result = converter.convert(groovy)
        assert "### Rule: MyRule (critical)" in result


# ---------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------

class TestStructure:

    def test_structure_without_annotation(self, converter):
        groovy = 'identity { role: "assistant" }'
        result = converter.convert(groovy)
        assert "## identity" in result

    def test_structure_with_annotation(self, converter):
        groovy = '@override identity { role: "assistant" }'
        result = converter.convert(groovy)
        assert "## identity (override)" in result


# ---------------------------------------------------------------------------
# property values
# ---------------------------------------------------------------------------

class TestPropertyValues:

    def test_string_property(self, converter):
        groovy = 'block { key: "hello world" }'
        result = converter.convert(groovy)
        assert "**key**" in result
        assert "hello world" in result

    def test_list_property_renders_bullets(self, converter):
        groovy = 'block { items: ["alpha", "beta", "gamma"] }'
        result = converter.convert(groovy)
        assert "alpha" in result
        assert "beta" in result
        assert "gamma" in result

    def test_multiline_string_strips_triple_quotes(self, converter):
        groovy = '''block { desc: """
This is a
multiline text.
""" }'''
        result = converter.convert(groovy)
        assert "This is a" in result
        assert '"""' not in result

    def test_multiline_string_single_quotes(self, converter):
        groovy = """block { desc: '''
Single quoted
multiline.
''' }"""
        result = converter.convert(groovy)
        assert "Single quoted" in result
        assert "'''" not in result


# ---------------------------------------------------------------------------
# comments and method_call
# ---------------------------------------------------------------------------

class TestIgnoredElements:

    def test_single_line_comment_not_in_output(self, converter):
        groovy = '''block {
// this is a comment
key: "value"
}'''
        result = converter.convert(groovy)
        assert "this is a comment" not in result
        assert "value" in result

    def test_method_call_not_in_output(self, converter):
        groovy = 'some.method()'
        result = converter.convert(groovy)
        # method_call renders as "" — output should be essentially empty/whitespace
        assert "some.method()" not in result


# ---------------------------------------------------------------------------
# timestamp preprocessing
# ---------------------------------------------------------------------------

class TestTimestampPreprocessing:

    def test_timestamp_line_re_prepended(self, converter):
        groovy = 'Current date and time is 2026-01-15 10:00 UTC\nblock { key: "val" }'
        result = converter.convert(groovy)
        assert result.startswith("Current date and time is 2026-01-15")

    def test_timestamp_line_not_duplicated_in_body(self, converter):
        groovy = 'Current date and time is 2026-01-15 10:00 UTC\nblock { key: "val" }'
        result = converter.convert(groovy)
        assert result.count("Current date and time") == 1

    def test_no_timestamp_line_unchanged(self, converter):
        groovy = 'block { key: "val" }'
        result = converter.convert(groovy)
        assert "Current date and time" not in result


# ---------------------------------------------------------------------------
# error fallback
# ---------------------------------------------------------------------------

class TestErrorFallback:

    def test_invalid_groovy_returns_original_prompt(self, converter):
        bad_input = "this is {{{ not valid groovy at all"
        result = converter.convert(bad_input)
        assert result == bad_input

    def test_completely_empty_input_does_not_raise(self, converter):
        result = converter.convert("")
        assert isinstance(result, str)
