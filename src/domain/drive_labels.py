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
# Decisions (recorded in §25.13.5, review MINOR 11):
#   - a multi-word entry ("hoja de cálculo", "web page") is ONE phrase key, its tokens' form (b) joined with "_" as
#     `e:` phrases are; never per-token keys (no `k:de`, no `k:page`); the query emits `k:` for its 1-3 token runs;
#   - `mhtml` is a web page (the `html` row); kinds with no row (subtitles, json, code, log, data_dump, database,
#     disk_image, executable, iwork, other) get no `k:` words;
#   - French words are those of the table (several coincide with en/es); more is a later KEYS_VERSION bump;
#   - photo words go to images whose verdict is `photo` only (§25.2.4's reading; owner to confirm);
#   - P1 review (KEYS_VERSION 2): image and photo rows gain the usual ru/uk/es/fr words (M7); the audio row says
#     "аудиозапись"/"аудіозапис" instead of "запись"/"запис", which covered every "записи" (m3); French added.
KIND_WORDS: Mapping[str, Tuple[str, ...]] = {
    "image": ("image", "picture", "imagen", "изображение", "зображення", "картинка", "illustration"),
    "pdf": ("pdf",),
    "docx": ("document", "documento", "документ"),
    "doc": ("document", "documento", "документ"),
    "odt": ("document", "documento", "документ"),
    "rtf": ("document", "documento", "документ"),
    "xlsx": ("spreadsheet", "table", "hoja de cálculo", "tableur", "таблица", "таблиця"),
    "xls": ("spreadsheet", "table", "hoja de cálculo", "tableur", "таблица", "таблиця"),
    "ods": ("spreadsheet", "table", "hoja de cálculo", "tableur", "таблица", "таблиця"),
    "csv": ("spreadsheet", "table", "hoja de cálculo", "tableur", "таблица", "таблиця"),
    "pptx": ("presentation", "slides", "presentación", "présentation", "презентация", "презентація"),
    "ppt": ("presentation", "slides", "presentación", "présentation", "презентация", "презентація"),
    "odp": ("presentation", "slides", "presentación", "présentation", "презентация", "презентація"),
    "markdown": ("note", "text", "nota", "texte", "заметка", "нотатка"),
    "text": ("note", "text", "nota", "texte", "заметка", "нотатка"),
    "html": ("web page", "página web", "page web", "веб-страница", "веб-сторінка"),
    "mhtml": ("web page", "página web", "page web", "веб-страница", "веб-сторінка"),
    "eml": ("email", "mail", "correo", "courriel", "письмо", "лист"),
    "msg": ("email", "mail", "correo", "courriel", "письмо", "лист"),
    "epub": ("book", "e-book", "libro", "livre", "книга"),
    "video": ("video", "vídeo", "vidéo", "видео", "відео"),
    "audio": ("audio", "recording", "grabación", "enregistrement", "аудио", "аудіо", "аудиозапись", "аудіозапис"),
    "archive": ("archive", "zip", "archivo", "архив", "архів"),
    "folder": ("folder", "carpeta", "dossier", "папка"),
}

# Added to an image's `k:` words only when its triage verdict is `photo` (§25.2.4, §25.13.5).
PHOTO_KIND_WORDS: Tuple[str, ...] = (
    "photo", "foto", "фото", "світлина", "photograph", "photographie", "fotografía", "fotografia", "фотография",
    "фотографія", "снимок", "знімок", "фотка",
)
