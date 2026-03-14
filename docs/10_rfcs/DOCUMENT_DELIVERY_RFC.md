# RFC: Unified Document Delivery + PDF Generation Pipeline

**Status:** In Progress
**Date:** 2026-03-14
**Owner:** Solo Dev
**Depends on:** [DELIVERY_ITEMS_RFC.md](DELIVERY_ITEMS_RFC.md) (Phase 1 — implemented)

---

## 1. Problem

### 1.1 Inconsistent file delivery

Phase 1 (`DELIVERY_ITEMS_RFC.md`) introduced `DeliveryItem` as a typed transport layer.
Two file delivery types emerged organically:

| Type | Path | Problem |
|------|------|---------|
| `"file_upload"` | bytes → Slack file upload (ephemeral) | Not stored in cloud. No permanent URL. |
| `"html_gcs_link"` | HTML → GCS → raw URL in Slack | URL is a kilometre long. Not human-readable. |

No unified storage layer. No filename convention. Slack links are unreadable URLs.

### 1.2 Slack-specific formatting leaks into ConversationHandler

`ConversationHandler._deliver_item` currently sends `<{url}|{link_text}>` — Slack mrkdwn
format — directly. This violates hexagonal architecture: the handler is coupled to Slack's
link syntax. A Telegram adapter would require a different format; there is no clean extension
point.

### 1.3 Filenames are not planned

`DocGeneratorAgent` auto-generates filenames as `{doc_type}-{date}.docx`. The planner,
which has full understanding of the document content, plays no role in naming. Short,
meaningful names require context the generator does not have.

---

## 2. Solution

### 2.1 New unified DeliveryItem type: `"document"`

```python
DeliveryItem(
    type="document",
    data={
        "content_b64":  "<base64 bytes>",
        "filename":     "q1_report.pdf",    # short, no spaces, with extension
        "content_type": "application/pdf",
        "label":        "Q1 Report.pdf",    # human-readable display name
        "file_upload":  True,               # also upload as Slack binary file (PDF only)
    }
)
```

**Delivery semantics:**
1. Store bytes to GCS via `MediaStoragePort` → get permanent URL
2. Send named link via `response_channel.send_document_link(url, label)` — platform-agnostic
3. If `file_upload=True`: also send binary file via `response_channel.send_file(...)` (PDF)

### 2.2 ResponseChannel: platform-agnostic link/file methods

New abstract methods on `ResponseChannel` (base class / protocol):

```python
async def send_document_link(self, url: str, label: str, thread_id: Optional[str]) -> None:
    """Send a named link to a document. Format is platform-specific."""

async def send_file(
    self, content: bytes, filename: str, title: str, thread_id: Optional[str]
) -> None:
    """Upload a binary file. Platform-specific upload mechanism."""
```

**SlackResponseChannel** implements:
```python
async def send_document_link(self, url, label, thread_id):
    await self.send_message(f"<{url}|{label}>", thread_id)   # Slack mrkdwn — only here

async def send_file(self, content, filename, title, thread_id):
    await self._client.files_upload_v2(...)                   # Slack file upload — only here
```

**TelegramResponseChannel** (future):
```python
async def send_document_link(self, url, label, thread_id):
    await self.send_message(f"[{label}]({url})", thread_id)  # Markdown link
```

`ConversationHandler` never formats links. It only calls `send_document_link` / `send_file`.

### 2.3 DocumentDeliveryService

New service (not a new port — `MediaStoragePort` already serves as the GCS port):

```python
# src/services/document_delivery_service.py

class DocumentDeliveryService:
    def __init__(self, storage: MediaStoragePort) -> None:
        self._storage = storage

    async def store(self, content: bytes, filename: str, content_type: str) -> str:
        """Upload to GCS, return public URL."""
        key = f"docs/{uuid4()}-{filename}"
        return await self._storage.store(data=content, key=key, content_type=content_type)
```

GCS key: `docs/{uuid}-{filename}`. UUID guarantees uniqueness; filename is informational.

**Not extending `RichContentService`:** different domain. `RichContentService` handles rich UI
(tables, widgets). `DocumentDeliveryService` handles file artifacts.

### 2.4 ConversationHandler: new `"document"` dispatch case

```python
elif item.type == "document":
    content = base64.b64decode(item.data["content_b64"])
    label = item.data.get("label", item.data["filename"])

    url = await self._doc_delivery_service.store(
        content, item.data["filename"], item.data["content_type"]
    )
    await response_channel.send_document_link(url=url, label=label, thread_id=thread_id)

    if item.data.get("file_upload"):
        await response_channel.send_file(
            content=content,
            filename=item.data["filename"],
            title=label,
            thread_id=thread_id,
        )
```

### 2.5 AgentWorkerHandler: async Cloud Tasks path

```python
if item.type == "document":
    content = base64.b64decode(item.data["content_b64"])
    url = await self._doc_delivery_service.store(
        content, item.data["filename"], item.data["content_type"]
    )
    label = item.data.get("label", item.data["filename"])
    await self._notification.notify_document_link(
        user_id=user_id, account_id=account_id, url=url, label=label
    )
    if item.data.get("file_upload"):
        await self._notification.notify_file_bytes(
            user_id=user_id, account_id=account_id,
            file_bytes=content,
            filename=item.data["filename"],
            title=label,
        )
```

`notify_document_link()` — new method on `UserNotificationService`. Platform-specific
link formatting stays in `SlackNotificationChannel` (implementation of `NotificationChannelPort`).

### 2.6 Filename convention

Planners include in spec:
```json
{ "filename": "q1_report" }
```
Rules: short (≤ 30 chars), no spaces (use underscores), no extension, English or transliterated.
Generator appends extension: `q1_report.pdf`, `q1_report.html`.

---

## 3. PDF Generation Pipeline

### 3.1 Architecture

```
User: "create a PDF report on..."
  → Quick/Smart → Intent.CREATE_PDF
    → PdfPlannerAgent (ASYNC, PERFORMANCE tier)
        → layout spec JSON (CSS units: mm/pt) + filename
        → delegates → Intent.GENERATE_PDF_CODE
    → PdfGeneratorAgent (ASYNC, internal, PERFORMANCE tier)
        → tool call: generate_html(html_code)
        → LLM writes complete HTML+CSS document
        → NodePuppeteerRunner.run(html_code) → PDF bytes
        → DeliveryItem "document" × 2:
            #1 HTML → GCS → Slack named link
            #2 PDF  → GCS → Slack named link + Slack file upload
```

### 3.2 PdfPlannerAgent

Mirror of `DocPlannerAgent`. Key differences:
- `agent_type = "doc_planner_pdf"` → picks up `doc_planner_pdf` Firestore profile
- Delegates `Intent.GENERATE_PDF_CODE` (not `GENERATE_DOCX_CODE`)
- Spec uses CSS units (mm/pt) instead of DXA
- Spec includes `filename` field

### 3.3 PdfGeneratorAgent

Mirror of `DocGeneratorAgent`. Key differences:
- Tool: `generate_html(html_code: str)` — LLM writes full HTML+CSS document
- LLM does NOT know about Puppeteer; the runner is a fixed Node.js wrapper
- Returns two `"document"` DeliveryItems (HTML + PDF)
- Retry loop: MAX_TURNS=5 (same as DOCX)

### 3.4 NodePuppeteerRunner (PuppeteerRunnerPort)

New port + adapter, following the same pattern as `DocxRunnerPort` / `NodeDocxRunner`:

```python
# src/ports/puppeteer_runner_port.py
class PuppeteerRunnerError(Exception): ...

class PuppeteerRunnerPort(ABC):
    @abstractmethod
    async def run(self, html_code: str, spec_json: str, timeout: int) -> bytes:
        """Render HTML to PDF bytes via Puppeteer."""
```

`NodePuppeteerRunner`: writes HTML to temp file in `pdf_generator/`, executes
`node pdf_generator/runner.js` with HTML via stdin, captures PDF bytes from stdout.

### 3.5 pdf_generator/ Node.js environment

New directory alongside `docx_generator/`:

**`pdf_generator/package.json`:**
```json
{
  "name": "pdf-generator",
  "version": "1.0.0",
  "description": "Puppeteer PDF renderer for LLM-generated HTML",
  "private": true,
  "dependencies": { "puppeteer": "^21.0.0" }
}
```

**`pdf_generator/runner.js`** (fixed, not LLM-generated):
```js
const puppeteer = require('puppeteer');

async function main() {
    let html = '';
    for await (const chunk of process.stdin) html += chunk;

    const browser = await puppeteer.launch({
        args: ['--no-sandbox', '--disable-setuid-sandbox']
    });
    const page = await browser.newPage();
    await page.setContent(html, { waitUntil: 'networkidle0' });
    const pdf = await page.pdf({ format: 'A4', printBackground: true });
    process.stdout.write(pdf);
    await browser.close();
}

main().catch(e => { process.stderr.write(String(e)); process.exit(1); });
```

### 3.6 Layout spec differences: PDF vs DOCX

| Field | DOCX spec | PDF spec |
|-------|-----------|----------|
| Page dimensions | `width_dxa`, `height_dxa` | `width_mm`, `height_mm` |
| Margins | `margins_dxa` (top/right/bottom/left) | `margins_mm` (top/right/bottom/left) |
| Primary color | `"1F3864"` (no #) | `"#1F3864"` (CSS-ready) |
| Font family | `"Arial"` | `"Arial, sans-serif"` |
| Page break | — | `page_break_before: bool` on sections |
| Filename | absent | `filename: "short_name"` |
| Quality rules | includes `google_docs_safe` | no `google_docs_safe` |
| Generator intent | `"generate_docx"` | `"generate_pdf"` |

### 3.7 Prompt tokens (Firestore uploads)

| File | Description |
|------|-------------|
| `DOC_PLANNER_COGNITIVE_PROCESS_PDF.groovy` | Fork of DOCX version: HTML/CSS semantics, mm/pt units, filename rule |
| `OUTPUT_FORMAT_DOC_PLANNER_PDF.groovy` | CSS units, `filename` field, `generator_handoff.intent: "generate_pdf"` |
| `pdf_planner_agent_v1.json` | Blueprint: `PdfPlannerAgent extends Agent`, sections `[cognitive_process, output_format]` |
| `doc_planner_pdf.json` | Profile: DOC_PLANNER_COGNITIVE_PROCESS_PDF (order=10) + OUTPUT_FORMAT_DOC_PLANNER_PDF (order=20) |
| `PDF_GENERATOR_COGNITIVE_PROCESS.groovy` | Role: HTML writer for Puppeteer. @page CSS, print media, page-break |
| `OUTPUT_FORMAT_PDF_GENERATOR.groovy` | Tool `generate_html(html_code)`, HTML5 contract, no external resources |
| `pdf_generator_agent_v1.json` | Blueprint |
| `pdf_generator.json` | Profile |

---

## 4. DOCX Migration Path

DocGeneratorAgent currently uses `"file_upload"` (direct Slack upload, no GCS copy).
Migration to `"document"` type is a separate task — not in this PR.

Backward compatibility: `"file_upload"` handler remains in `ConversationHandler`. Both types
coexist. Migration = change one line in `DocGeneratorAgent._build_delivery_items()`.

---

## 5. Files Touched

### New files
- `docs/10_rfcs/DOCUMENT_DELIVERY_RFC.md` (this file)
- `src/services/document_delivery_service.py`
- `src/ports/puppeteer_runner_port.py`
- `src/adapters/node_puppeteer_runner.py`
- `src/agents/pdf_planner_agent.py`
- `src/agents/pdf_generator_agent.py`
- `pdf_generator/package.json`
- `pdf_generator/runner.js`
- `firestore_utils/uploads/DOC_PLANNER_COGNITIVE_PROCESS_PDF.groovy`
- `firestore_utils/uploads/OUTPUT_FORMAT_DOC_PLANNER_PDF.groovy`
- `firestore_utils/uploads/pdf_planner_agent_v1.json`
- `firestore_utils/uploads/doc_planner_pdf.json`
- `firestore_utils/uploads/PDF_GENERATOR_COGNITIVE_PROCESS.groovy`
- `firestore_utils/uploads/OUTPUT_FORMAT_PDF_GENERATOR.groovy`
- `firestore_utils/uploads/pdf_generator_agent_v1.json`
- `firestore_utils/uploads/pdf_generator.json`

### Modified files
- `src/adapters/slack/response_channel.py` — `send_document_link()`, `send_file()`
- `src/handlers/conversation_handler.py` — `"document"` case in `_deliver_item()`
- `src/handlers/agent_worker_handler.py` — `"document"` case
- `src/services/user_notification_service.py` — `notify_document_link()`
- `src/infrastructure/agent_manifest.py` — `Intent.CREATE_PDF`, `Intent.GENERATE_PDF_CODE`, 2 descriptors
- `src/infrastructure/agent_config.py` — `PDF_PLANNER`, `PDF_GENERATOR` configs
- `src/composition/service_container.py` — wiring

---

## 6. Non-goals

- Telegram adapter changes (same `delivery_items` list; Telegram will implement `send_document_link` when needed)
- DOCX migration to `"document"` type — separate task
- `"html_gcs_link"` and `"file_upload"` removal — not in this RFC (backward compat)
- PDF rendering quality tuning — separate iteration after initial working version
