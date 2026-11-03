"""Span exporter that keeps file contents out of exported LLM spans.

The provider-SDK instrumentation puts the whole request on one attribute (`gen_ai.input.messages`), file bytes
included. A span processor cannot edit a finished span's attributes (they are a read-only mapping), so the
redaction happens at export time: this wrapper hands the inner exporter sanitised copies and never touches the live
span. A span without a file part reaches the inner exporter untouched.

Every part whose `type` is not a text, tool or reasoning type is replaced by
`{"type": "file_omitted", "orig_type": ..., "mime_type": ...}`. The three SDK shapes seen in practice: Anthropic
`document` / `image` with `source.base64`, OpenAI and Grok `blob` with a `data:` URL, Gemini `blob` with `content`.
"""
import json
from typing import Any, Optional, Sequence

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

_KEEP_TYPES = frozenset({
    "text", "tool_call", "tool_call_response", "reasoning", "thinking", "redacted_thinking",
    "tool_use", "tool_result", "server_tool_use",
})
_MESSAGE_KEYS = ("gen_ai.input.messages", "gen_ai.output.messages", "gen_ai.system_instructions")
_SPAN_TYPE_KEY = "logfire.span_type"


def _mime_type(part: dict) -> Optional[str]:
    mime = part.get("mime_type")
    if isinstance(mime, str):
        return mime
    source = part.get("source")
    if isinstance(source, dict) and isinstance(source.get("media_type"), str):
        return source["media_type"]
    content = part.get("content")
    if isinstance(content, str) and content.startswith("data:"):
        return content[5:].split(";", 1)[0].split(",", 1)[0] or None
    return None


def _scrub_part(part: Any) -> Any:
    if isinstance(part, dict) and part.get("type") not in _KEEP_TYPES:
        return {"type": "file_omitted", "orig_type": part.get("type"), "mime_type": _mime_type(part)}
    return part


def _scrub_items(items: list) -> list:
    """A list of messages (each with `parts`) or, for system instructions, a plain list of parts."""
    out = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("parts"), list):
            item = {**item, "parts": [_scrub_part(p) for p in item["parts"]]}
        else:
            item = _scrub_part(item)
        out.append(item)
    return out


def redact_json(value: str) -> str:
    """The attribute value with its file parts omitted; the same string when there is nothing to omit or it is not JSON."""
    try:
        data = json.loads(value)
    except (ValueError, TypeError):
        return value
    if not isinstance(data, list):
        return value
    scrubbed = _scrub_items(data)
    if scrubbed == data:
        return value
    return json.dumps(scrubbed, separators=(",", ":"), ensure_ascii=False)


class RedactingExporter(SpanExporter):
    """Wraps any exporter; `drop_pending` also drops Logfire's pending-span records (as its own exporter does)."""

    def __init__(self, inner: SpanExporter, *, drop_pending: bool = False) -> None:
        self._inner = inner
        self._drop_pending = drop_pending

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if self._drop_pending:
            spans = self._without_pending(spans)
        return self._inner.export([self._sanitised(s) for s in spans])

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)

    @staticmethod
    def _without_pending(spans: Sequence[ReadableSpan]) -> list:
        """Drop a pending record whose finished span is in the same batch."""
        finished = {
            (s.context.trace_id, s.context.span_id)
            for s in spans
            if s.context is not None and (s.attributes or {}).get(_SPAN_TYPE_KEY) == "span"
        }
        kept = []
        for s in spans:
            if (s.attributes or {}).get(_SPAN_TYPE_KEY) == "pending_span" and s.parent is not None:
                if (s.parent.trace_id, s.parent.span_id) in finished:
                    continue
            kept.append(s)
        return kept

    @staticmethod
    def _sanitised(span: ReadableSpan) -> ReadableSpan:
        attrs = span.attributes or {}
        changed = {}
        for key in _MESSAGE_KEYS:
            value = attrs.get(key)
            if isinstance(value, str):
                new = redact_json(value)
                if new is not value and new != value:
                    changed[key] = new
        if not changed:
            return span
        return ReadableSpan(
            name=span.name, context=span.context, parent=span.parent, resource=span.resource,
            attributes={**attrs, **changed}, events=span.events, links=span.links, kind=span.kind,
            status=span.status, start_time=span.start_time, end_time=span.end_time,
            instrumentation_scope=span.instrumentation_scope,
        )
