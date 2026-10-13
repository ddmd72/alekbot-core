"""
Drive labels — the fixed label set of Search v2 (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §25.2.3, D15) and the `k:`
kind-word table (§25.13.5). Pure constants.

Closed sets: the profiler may not add values; a value outside its set becomes `other` or is dropped (§25.4.1). There
is no query-time synonym table: the words a user may say for a label, a topic or a life area are written per file at
index time (P8). `LIFE_DOMAINS` is its own set, not `FactDomain`.

Public interface:
    DOC_TYPES, LIFE_DOMAINS, DATE_LABELS, ENTITY_TYPES, VALUE_LABELS      (ordered tuples, as listed in §25.2.3)
    KIND_WORDS: kind -> words (the `k:` table, before §25.2.4's forms);  PHOTO_KIND_WORDS (images, verdict `photo`)
"""
from typing import Mapping, Tuple

DOC_TYPES: Tuple[str, ...] = (
    "receipt", "invoice", "bill", "bank_statement", "payslip", "tax_document", "contract", "insurance_policy",
    "warranty", "ticket", "booking", "certificate", "id_document", "medical_record", "prescription", "fine", "letter",
    "form", "manual", "book", "article", "report", "plan", "notes", "recipe", "newsletter", "web_page", "spreadsheet",
    "data", "presentation", "other",
)

LIFE_DOMAINS: Tuple[str, ...] = (
    "finance", "health", "travel", "home", "vehicle", "work", "education", "legal_admin", "shopping", "leisure",
    "family", "tech", "news_reference", "other",
)

DATE_LABELS: Tuple[str, ...] = ("issued", "event", "valid_from", "valid_until", "due", "period_end", "other")

ENTITY_TYPES: Tuple[str, ...] = ("place", "organization", "person", "event", "product", "work", "other")

VALUE_LABELS: Tuple[str, ...] = (
    "order_number", "ticket_number", "booking_reference", "invoice_number", "receipt_number", "account_number", "iban",
    "card_number_masked", "policy_number", "contract_number", "customer_number", "reference", "tax_id", "phone",
    "amount_total", "amount_due", "price", "other_id",
)

# The `k:` table (§25.13.5): the record's kind names in en, es, fr, uk and ru, written by the keyer at index time and
# passed through §25.2.4's forms there. Keyed by `drive_index_policy.kind_of` kinds. Index side only.
KIND_WORDS: Mapping[str, Tuple[str, ...]] = {
    "image": ("image", "picture", "imagen"),
    "pdf": ("pdf",),
    "docx": ("document", "documento", "документ"),
    "doc": ("document", "documento", "документ"),
    "odt": ("document", "documento", "документ"),
    "rtf": ("document", "documento", "документ"),
    "xlsx": ("spreadsheet", "table", "hoja de cálculo", "таблица", "таблиця"),
    "xls": ("spreadsheet", "table", "hoja de cálculo", "таблица", "таблиця"),
    "ods": ("spreadsheet", "table", "hoja de cálculo", "таблица", "таблиця"),
    "csv": ("spreadsheet", "table", "hoja de cálculo", "таблица", "таблиця"),
    "pptx": ("presentation", "slides", "presentación", "презентация", "презентація"),
    "ppt": ("presentation", "slides", "presentación", "презентация", "презентація"),
    "odp": ("presentation", "slides", "presentación", "презентация", "презентація"),
    "markdown": ("note", "text", "nota", "заметка", "нотатка"),
    "text": ("note", "text", "nota", "заметка", "нотатка"),
    "html": ("web page", "página web", "веб-страница", "веб-сторінка"),
    "eml": ("email", "mail", "correo", "письмо", "лист"),
    "msg": ("email", "mail", "correo", "письмо", "лист"),
    "epub": ("book", "e-book", "libro", "книга"),
    "video": ("video", "vídeo", "видео", "відео"),
    "audio": ("audio", "recording", "grabación", "запись", "запис"),
    "archive": ("archive", "zip", "archivo", "архив", "архів"),
    "folder": ("folder", "carpeta", "папка"),
}

# Added to an image's `k:` words only when its triage verdict is `photo` (§25.2.4, §25.13.5).
PHOTO_KIND_WORDS: Tuple[str, ...] = ("photo", "foto", "фото", "світлина")
