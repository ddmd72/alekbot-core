# File Conversion Service

## Purpose

Converts file attachments to text **before** passing them to LLM adapters.

Goal — LLM-agnosticism: convert once, works for Claude, Gemini, Grok without changes in adapters.

---

## Place in Architecture

```
Platform Adapter (Slack/Telegram)
        │ FileAttachment(url, mime_type, filename)
        ▼
ConversationHandler
        │
        ├── FileConversionService present? (GCS path — preferred)
        │       YES → process_attachment() → GCS upload → reference-only MessagePart
        │             (no file content in history — see file_storage building block)
        │
        ├── is_native_binary(mime_type)? (legacy fallback)
        │       YES → MessagePart(file_data) → adapters handle natively
        │       NO  → convert_file_to_text() → MessagePart(text)
        │
        ▼
AgentCoordinator → RouterAgent → Quick/Smart/WebSearch
        │
        ├── delegate with file_ref? → _resolve_file_refs() → download + convert → inject file_content
        └── open_file intent? → FileManagementAgent → resolve_content/download
```

Layer: **`src/utils/`** — pure utility functions (no class, no port, no I/O).

> **See also:** [../file_storage/README.md](../file_storage/README.md) — GCS-backed file storage,
> `FileConversionService`, and `FileManagementAgent`.

---

## Files

- `src/utils/file_conversion.py` — all conversion logic (moved from `src/services/file_conversion_service.py` in 2026-03-08 hexagonal audit)
- `src/handlers/conversation_handler.py` — call site (lines 245–258)

---

## Conversion Logic

### Binary formats (bypass the service)

```python
is_native_binary(mime_type: str) -> bool
```

Returns `True` for `image/*` and `application/pdf`.
These types are passed to adapters as `MessagePart(file_data=...)` — adapters know what to do with them.

### Text formats (fast path)

MIME `text/*` (txt, csv, md, html, …) — direct read as UTF-8, **without markitdown**.
No external dependencies, no risks.

### Binary convertible formats

DOCX, XLSX, and everything markitdown supports — via `MarkItDown.convert(path)`.
Lazy import: `from markitdown import MarkItDown` inside the function — startup does not fail if the library is not installed.

### Graceful degradation via LLM alerts

Any conversion error → **system alert as text** injected into the message, not an exception:

```
[System: User attempted to attach 'file.xyz' (application/octet-stream).
The file could not be read or is not a supported text format.
Supported formats: images, PDF, plain text, CSV, DOCX, XLSX.
Ask the user to convert the file or paste the content directly.]
```

Technical details (exception type, path) — in logs only.

#### Why this is the right pattern

Failure becomes a **conversation prompt**, not a technical error.

The LLM sees `[System: ...]` as a directive in the context and responds to the user in natural language — explains what happened, suggests alternatives. The user gets a helpful response instead of silence or a bare error message.

Three failure scenarios, three distinct alerts:

| Scenario | Alert | What the LLM will say |
|---|---|---|
| File > 5 MB | `_size_alert()` | Will ask for a smaller file or to paste text directly |
| Unsupported format / conversion error | `_conversion_alert()` | Will explain supported formats, suggest converting |
| Audio file (transcription not configured) | inline alert | Will ask the user to send text instead of audio |
| File truncated (> 30K chars) | truncation alert | Will inform that the file was too large for full analysis, suggest splitting |

#### Alternatives that were rejected

- **Exception / empty response** — the user gets nothing and does not understand why
- **Bot-level error message** ("I cannot process this file") — breaks conversational flow, feels mechanical
- **Silent data loss** (skip with warning in logs) — user assumes the file was processed

LLM alert — least friction: user does not notice the technical failure, the conversation continues.

---

## Limits

| Parameter | Value | Reason |
|---|---|---|
| `MAX_FILE_BYTES` | 5 MB | Context window protection |
| `MAX_CONVERTED_CHARS` | 30 000 | ~7K tokens — hard truncation |
| `HISTORY_PREVIEW_CHARS` | 1 000 | chars in stub for history |

Size exceeded → `_size_alert()` with instruction to send a smaller file.
Text length exceeded → truncation to `MAX_CONVERTED_CHARS` + system alert for LLM:
```
[System: File 'report.docx' was truncated — only the first 30,000 characters were included
(5,234 characters omitted). Inform the user that the file was too large for full analysis
and suggest splitting it or pasting only the relevant section.]
```

---

## Dependencies

```
requirements.txt:
markitdown[all]   # [all] required: onnxruntime (69MB) describes images inside DOCX/XLSX
                  # without it, images in documents are ignored
```

Includes: python-docx, openpyxl, onnxruntime (image description in docs), speechrecognition, youtube-transcript-api.
The last two are not currently used (see AudioTranscriptionPort section below).

---

## Output Format

Successful conversion:
```
[File: filename.docx]
{extracted text content}
[/File: filename.docx]
```

The closing marker lets the LLM clearly see the file/prompt boundary.

---

## History Storage (tiered history)

Files are not stored in full in session history — that would be dead weight on every subsequent request.

**Mechanism:** `ConversationHandler` saves `MessagePart` with two fields:

```python
MessagePart(
    text=stub,          # 1 000 chars — always carried in history
    full_text=full,     # 30 000 chars — used for the first 5 turns
)
```

**`make_history_stub(full_output, filename)`** — creates a compact stub:
- Keeps the first `HISTORY_PREVIEW_CHARS = 1 000` characters of content
- Preserves `[File:] / [/File:]` markers
- Short files (≤ 1 000 chars) — no truncation

**`BaseAgent._apply_history_tier()`** decides what to pass to the LLM:
- ≤ 5 most recent model turns: `full_text` (full content)
- Older than 5 turns: `text` (stub — LLM knows the file existed, but does not carry it in full)

Works for both agents (Quick and Smart) through the base class.

---

## Supported Formats

| Type | Path | Tool |
|---|---|---|
| `image/*` | native binary | adapter (Claude vision / Gemini) |
| `application/pdf` | native binary | adapter (Claude / Gemini) |
| `text/*` (txt, csv, md…) | fast path | stdlib read |
| `application/vnd.openxmlformats…docx` | markitdown | python-docx |
| `.xlsx` | markitdown | openpyxl |
| `audio/*` (mp3, wav, m4a, ogg) | AudioTranscriptionPort | not connected — system alert |
| Unsupported | graceful degradation | system alert |

---

## Why ConversationHandler, not adapters

The alternative — conversion inside each adapter (Claude, Gemini, Grok).
Rejected: duplicated logic, diverging behavior for the same data, harder to test.

Text output — the lowest common denominator, works everywhere.

---

## AudioTranscriptionPort (port ready, adapter not connected)

Audio files (`audio/mpeg`, `audio/wav`, `audio/mp4`, `audio/x-m4a`, `audio/ogg`) are detected in `convert_file_to_text()` in a dedicated branch. Transcription is delegated behind a port — `AudioTranscriptionPort` (`src/ports/audio_transcription_port.py`).

**Current state:** `audio_service=None` everywhere → user receives an alert "transcription unavailable, please send text".

**What was tried and why disabled:**
- `SpeechRecognitionAdapter` (markitdown → Google Web Speech API, free): English-only, `UnknownValueError` on Russian/Ukrainian, ~50 requests/day

**To enable:** write a `WhisperAdapter` or `GoogleCloudSpeechAdapter` implementing the port, and pass it into `main.py`. The DI chain (`audio_service` parameter in factory → adapters → ConversationHandler) is ready, nothing needs to be rewritten. See the port's docstring for details.

---

## History

Added: 2026-02-19
Reason: Claude API returned HTTP 400 for unsupported MIME types (docx, xlsx, txt passed as `document`).
The initial fix (skip with warning) was rejected — silent data loss.
Chosen solution — graceful degradation with system alert.
