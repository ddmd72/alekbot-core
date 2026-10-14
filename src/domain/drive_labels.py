"""
Drive labels — the fixed label set of Search v2 (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §25.2.3, D15) and the `k:`
kind-word table (§25.13.5). Pure constants.

Closed sets: the profiler may not add values; a value outside its set becomes `other` or is dropped (§25.4.1). There
is no query-time synonym table: the words a user may say for a label, a topic or a life area are written per file at
index time (P8). `LIFE_DOMAINS` is its own set, not `FactDomain`.

Public interface:
    DOC_TYPES, LIFE_DOMAINS, DATE_LABELS, ENTITY_TYPES, VALUE_LABELS      (ordered tuples, as listed in §25.2.3)
    KIND_WORDS: kind -> words (the `k:` table, before §25.2.4's forms);  PHOTO_KIND_WORDS (images, verdict `photo`)
    DOC_TYPE_NAMES, LIFE_DOMAIN_NAMES: label value -> its names (D31, keyed `lw:` / `dw:` at index time)
    label_names_digest() -> str                  pinned by a test (a change is a KEYS_VERSION bump)
"""
import hashlib
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


# D31 (arbiter, 2026-10-13): the names of our own fixed labels in en, es, uk and ru, singular and plural, written by the
# keyer beside the profiler's `label_words` (`lw:`, doc types) and `domain_words` (`dw:`, life areas), as the `k:` kind
# table is. It names the enum values the profiler chose; it is NOT a query-side synonym table (D28 stands): a query
# word still meets only what the index wrote for that file. A multi-word name is keyed per token (as label words are).
DOC_TYPE_NAMES: Mapping[str, Tuple[str, ...]] = {
    "receipt": ("receipt", "receipts", "recibo", "recibos", "квитанція", "квитанції", "чек",
                "чеки", "квитанция", "квитанции"),
    "invoice": ("invoice", "invoices", "factura", "facturas", "рахунок-фактура", "рахунки-фактури", "інвойс",
                "інвойси", "счёт-фактура", "счета-фактуры", "инвойс", "инвойсы"),
    "bill": ("bill", "bills", "utility bill", "factura", "facturas", "рахунок", "рахунки", "счёт", "счета"),
    "bank_statement": ("bank statement", "bank statements", "extracto bancario", "extractos bancarios",
                       "банківська виписка", "банківські виписки", "банковская выписка", "банковские выписки"),
    "payslip": ("payslip", "payslips", "pay slip", "nómina", "nóminas", "розрахунковий лист", "розрахункові листи",
                "расчётный лист", "расчётные листы"),
    "tax_document": ("tax document", "tax documents", "tax return", "documento fiscal", "documentos fiscales",
                     "declaración de la renta", "податкова декларація", "податкові декларації",
                     "налоговая декларация", "налоговые декларации"),
    "contract": ("contract", "contracts", "agreement", "agreements", "contrato", "contratos", "договір", "договори",
                 "контракт", "контракти", "договор", "договоры", "контракты"),
    "insurance_policy": ("insurance policy", "insurance policies", "insurance", "póliza de seguro", "pólizas de seguro",
                         "seguro", "seguros", "страховий поліс", "страхові поліси", "страховка", "страховой полис",
                         "страховые полисы"),
    "warranty": ("warranty", "warranties", "guarantee", "garantía", "garantías", "гарантія", "гарантії",
                 "гарантия", "гарантии"),
    "ticket": ("ticket", "tickets", "entrada", "entradas", "billete", "billetes", "квиток", "квитки", "билет",
               "билеты"),
    "booking": ("booking", "bookings", "reservation", "reservations", "reserva", "reservas", "бронювання", "бронь",
                "бронирование", "бронирования"),
    "certificate": ("certificate", "certificates", "certificado", "certificados", "сертифікат", "сертифікати",
                    "свідоцтво", "свідоцтва", "довідка", "довідки", "сертификат", "сертификаты", "свидетельство",
                    "свидетельства", "справка", "справки"),
    "id_document": ("identity document", "identity documents", "passport", "passports", "ID card", "ID cards",
                    "documento de identidad", "documentos de identidad", "pasaporte", "pasaportes", "DNI",
                    "посвідчення особи", "паспорт", "паспорти", "удостоверение личности", "паспорта"),
    "medical_record": ("medical record", "medical records", "health record", "health records", "historial médico",
                       "historiales médicos", "historia clínica", "informe médico", "informes médicos",
                       "медичний запис", "медичні записи", "медична картка", "медичні картки",
                       "медицинская запись", "медицинские записи", "медицинская карта", "медицинские карты"),
    "prescription": ("prescription", "prescriptions", "receta médica", "recetas médicas", "рецепт", "рецепти",
                     "рецепты"),
    "fine": ("fine", "fines", "penalty", "penalties", "multa", "multas", "штраф", "штрафи", "штрафы"),
    "letter": ("letter", "letters", "carta", "cartas", "лист", "листи", "письмо", "письма"),
    "form": ("form", "forms", "formulario", "formularios", "бланк", "бланки", "анкета", "анкети", "анкеты"),
    "manual": ("manual", "manuals", "user guide", "instructions", "manuales", "instrucciones", "інструкція",
               "інструкції", "посібник", "посібники", "инструкция", "инструкции", "руководство", "руководства"),
    "book": ("book", "books", "libro", "libros", "книга", "книги", "книжка", "книжки"),
    "article": ("article", "articles", "artículo", "artículos", "стаття", "статті", "статья", "статьи"),
    "report": ("report", "reports", "informe", "informes", "звіт", "звіти", "отчёт", "отчёты"),
    "plan": ("plan", "plans", "planes", "план", "плани", "планы"),
    "notes": ("note", "notes", "nota", "notas", "нотатка", "нотатки", "заметка", "заметки"),
    "recipe": ("recipe", "recipes", "receta", "recetas", "рецепт", "рецепти", "рецепты"),
    "newsletter": ("newsletter", "newsletters", "boletín", "boletines", "розсилка", "розсилки", "рассылка",
                   "рассылки"),
    "web_page": ("web page", "web pages", "página web", "páginas web", "веб-сторінка", "веб-сторінки",
                 "веб-страница", "веб-страницы"),
    "spreadsheet": ("spreadsheet", "spreadsheets", "hoja de cálculo", "hojas de cálculo", "таблиця", "таблиці",
                    "таблица", "таблицы"),
    "data": ("data", "dataset", "datasets", "datos", "conjunto de datos", "дані", "набір даних", "данные",
             "набор данных"),
    "presentation": ("presentation", "presentations", "presentación", "presentaciones", "презентація", "презентації",
                     "презентация", "презентации"),
    "other": (),
}

LIFE_DOMAIN_NAMES: Mapping[str, Tuple[str, ...]] = {
    "finance": ("finance", "finances", "money", "finanzas", "dinero", "фінанси", "гроші", "финансы", "деньги"),
    "health": ("health", "medical", "salud", "médico", "здоровʼя", "медицина", "здоровье"),
    "travel": ("travel", "trip", "trips", "viaje", "viajes", "подорож", "подорожі", "поїздка", "поїздки",
               "путешествие", "путешествия", "поездка", "поездки"),
    "home": ("home", "house", "housing", "hogar", "casa", "vivienda", "дім", "житло", "квартира", "дом", "жильё"),
    "vehicle": ("vehicle", "vehicles", "car", "cars", "vehículo", "vehículos", "coche", "coches", "автомобіль",
                "автомобілі", "машина", "машини", "автомобиль", "автомобили", "машины"),
    "work": ("work", "job", "employment", "trabajo", "empleo", "робота", "работа"),
    "education": ("education", "school", "studies", "educación", "estudios", "escuela", "освіта", "навчання",
                  "школа", "образование", "учёба"),
    "legal_admin": ("legal", "administration", "paperwork", "trámites", "administración", "юридичні справи",
                    "адміністративні справи", "юридические дела", "административные дела"),
    "shopping": ("shopping", "purchase", "purchases", "compras", "compra", "покупка", "покупки"),
    "leisure": ("leisure", "hobby", "hobbies", "entertainment", "ocio", "entretenimiento", "дозвілля", "хобі",
                "розваги", "досуг", "хобби", "развлечения"),
    "family": ("family", "families", "familia", "familias", "сімʼя", "родина", "семья"),
    "tech": ("tech", "technology", "computer", "computers", "software", "tecnología", "informática", "технології",
             "компʼютер", "технологии", "компьютер"),
    "news_reference": ("news", "reference", "noticias", "referencia", "новини", "новости"),
    "other": (),
}


def label_names_digest() -> str:
    """SHA-256 of the D31 tables: a test pins it, so a change fails until KEYS_VERSION moves with it."""
    tables = (sorted(DOC_TYPE_NAMES.items()), sorted(LIFE_DOMAIN_NAMES.items()))
    return hashlib.sha256(repr(tables).encode("utf-8")).hexdigest()
