# Gemini prompts now reach Logfire — three defects, not one

**Date:** 2026-08-15
**Status:** Implemented
**Scope:** `requirements.txt`, `src/utils/telemetry.py`

## Context

Gemini traffic was metadata-only in Logfire: no span, no prompt, no response. The
recorded reason was a dependency pin conflict, and `telemetry.py` documented it as a
known gap that would "self-enable once upstream relaxes the pin".

It became urgent when the router moved to Gemini (`router_gemini_ab_and_eco_pinning.md`)
— that is one completely untraced LLM call on **every user message**.

Investigating found the documented reason was stale, and that it had been hiding two
further defects underneath it. Each alone was sufficient to keep content out.

## Defect 1 — the pin conflict, which had already cleared

`opentelemetry-instrumentation-google-genai` needs `opentelemetry-api~=1.43`;
logfire 4.34 pinned `opentelemetry-sdk<1.42`. Genuinely un-co-installable when written.

logfire 4.40 widened to `opentelemetry-sdk<1.45`, which admits 1.43. Verified by
resolving and installing the full set in a clean venv, including that
`opentelemetry-exporter-gcp-trace` 1.14.0 still imports on sdk 1.43.0 — the reason the
OTel versions are pinned as a set is a 30-minute pip backtrack in clean builds, so the
set moves together or not at all.

## Defect 2 — our own value was invalid

```python
os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
```

It is **not a boolean**. Valid values are `NO_CONTENT` / `SPAN_ONLY` / `EVENT_ONLY` /
`SPAN_AND_EVENT`. The instrumentation rejects anything else with a warning and falls
back to `NO_CONTENT`:

```
true is not a valid option for `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`
environment variable. Must be one of NO_CONTENT, SPAN_ONLY, EVENT_ONLY,
SPAN_AND_EVENT. Defaulting to `NO_CONTENT`.
```

So even if the pins had aligned earlier, content would still have been empty — and the
blame would have gone to the pins. Worse, the unit test **asserted `== "true"`**: the
suite was pinning the defect in place. Now `SPAN_ONLY`, with tests that assert the value
is one the instrumentation accepts and is not a boolean string, rather than asserting a
literal.

## Defect 3 — logfire's own shim is broken for the current semconv

`logfire.instrument_google_genai()` installs `SpanEventLogger`, whose `emit` does:

```python
assert isinstance(record.body, dict)
```

It expects the OLD per-message GenAI events (`gen_ai.choice`, `gen_ai.*.message`).
`opentelemetry-util-genai` 1.0b0 emits ONE consolidated
`gen_ai.client.inference.operation.details`. The assert fails on every call.

It is not an outage: `@handle_internal_errors` swallows it, so calls succeed. The
symptom is silently missing telemetry plus one internal-error line per call — the worst
shape of failure, because everything looks like it is working.

**Fix: drive the vendor instrumentor directly**, skipping logfire's wrapper:

```python
from opentelemetry.instrumentation.google_genai import GoogleGenAiSdkInstrumentor
GoogleGenAiSdkInstrumentor().instrument()
```

Nothing is lost by skipping the shim — it exists to translate into logfire's display
format, and the standard OTel GenAI attributes the vendor instrumentor writes are what
the Logfire LLM panels read anyway (the same reason `version="latest"` is passed to the
Anthropic and OpenAI instrumentors).

## Verified

Through the repo's own `_instrument_llm_sdks`, live against the API:

```
gen_ai.input.messages  = [{"role":"user","parts":[{"content":"Name one Ukrainian city...
gen_ai.output.messages = [{"role":"assistant","parts":[{"content":"Kyiv"}],"finish_reason":"stop"}]
gen_ai.request.model   = gemini-3.5-flash-lite
gen_ai.usage.input_tokens = 9
```

`SPAN_ONLY` over `SPAN_AND_EVENT`: both put identical content on the span; the event
adds only a log record with a null body.

## Consequences

- Gemini calls appear in the Logfire trace tree with full prompt and response. The
  router's per-message triage is no longer a hole in the cascade.
- BigQuery `prompt_content` remains the durable store (30-day TTL vs Logfire's 14-day
  query window). The split in `logfire_prompt_content_capture.md` is unchanged;
  Gemini simply stops being the exception to it.
- `opentelemetry-instrumentation-google-genai` is `1.0b1` — beta by upstream's own
  versioning. Tracing is diagnostic and each instrumentor is individually try/except'd,
  so a future break degrades telemetry, not traffic.
- If logfire later fixes its shim, `logfire.instrument_google_genai()` becomes viable
  again — but there is no reason to switch back.
