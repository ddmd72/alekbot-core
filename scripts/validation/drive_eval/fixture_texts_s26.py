"""
The words and the synthetic enrichment of the §26 / D39 / D40 additions to the Search v2 overlay
(USER_DRIVE_INDEXING_RFC §26.8 Eval; tests/fixtures/drive_eval/tree_v2.json, files in files_v2/). Every person, number,
document and address is fictional; the card, the permit and the meter are marked SAMPLE on their face.

What the overlay gains, and the case each file serves (gold_v2.json, gold_g3_v2.json):
  - an ID-card-like SCAN (a full-page raster PDF at 200 dpi, no EXIF, no text layer) printing its dates the way a
    card does, with spaces (`07 05 2031`), and a document number (`QX 4471902`): "scans", the expiry-date query in two
    spellings, the number query, and the G3 value question on a card under 90 % accuracy;
  - a phone PHOTO of a document (a JPEG whose EXIF names a camera): `document_photo`, detected;
  - a SCREENSHOT (a PNG): `screenshot`, said by the model, so keyed `cpa:`;
  - an INACCESSIBLE file (listed by delta, 404 on every id call): returned with the access line (§26.2);
  - three photographs (D40 scene labels, D39 location): a sunset, a beach, and a picture taken near a public landmark.

`ENRICHMENT` is what a live run might have answered for them, written by hand (`synthetic_s26.py` puts it into
`enrichment.json`, every entry marked `"synthetic": true`): the owner's live re-record
(`dump_enrichment.py --fixture`) replaces it with real model answers.
"""
from __future__ import annotations

from typing import Any, Dict, List

# -- the pages ---------------------------------------------------------------------------------------------------------

ID_CARD_LINES: List[str] = [
    "IDENTITY CARD",
    "SAMPLE - NOT A REAL DOCUMENT",
    "Name: SAMPLE PERSON EXAMPLE",
    "Date of birth: 03 11 1990",
    "Document no.: QX 4471902",
    "Issued: 07 05 2021",
    "Valid until: 07 05 2031",
]

METER_CARD_LINES: List[str] = [
    "METER READING CARD",
    "SAMPLE - NOT A REAL DOCUMENT",
    "Meter no. GM-30517",
    "Reading: 04412",
    "Date: 02/07/2026",
]

LOAN_SCREEN_LINES: List[str] = [
    "Example Library",
    "Loan reminder",
    "Item: Atlas of Birds",
    "Return by 24/09/2026",
]

CHIMNEY_CERT_LINES: List[str] = [
    "CHIMNEY INSPECTION CERTIFICATE",
    "Certificate no. CH-2026-0073",
    "Inspected by Example Sweeps Ltd on 18/08/2026",
    "Result: flue clear, no defects found",
]

# -- the model's (synthetic) answers -----------------------------------------------------------------------------------

_VALIDITY_WORDS = ["valid until", "expires", "expiry date", "válido hasta", "caduca", "vence", "діє до",
                   "термін дії", "закінчується", "действует до", "истекает", "срок действия"]


def _text(lines: List[str]) -> str:
    return "\n".join(line for line in lines if line)


def _outcome(lines: List[str], doc_type: str, description: str, doc_date: str, parties: List[str],
             amount: str = "") -> Dict[str, Any]:
    """The reader's recorded shape (`enrichment_snapshot.outcome_to_json`)."""
    return {"status": "text", "text": _text(lines), "pages_total": 1, "pages_sent": 1, "truncated": False,
            "brief": {"doc_type": doc_type, "description": description, "doc_date": doc_date, "parties": parties,
                      "amount": amount},
            "extracted_by": "reader", "pages_refused": 0, "model_calls": 0, "model_cost": 0.0,
            "script_rendered": False}


def _forms(en: str, es: str, uk: str, ru: str) -> List[str]:
    return [es, uk, ru, en]


ENRICHMENT: Dict[str, Dict[str, Any]] = {
    # path -> {"reader": outcome, "profile": profile (before verification), "photo": triage entry}
    "Documents/DOC_20260610_0042.pdf": {
        "reader": _outcome(ID_CARD_LINES, "id_document", "Scanned identity card of Sample Person Example.",
                           "2021-05-07T00:00:00+00:00", ["SAMPLE PERSON EXAMPLE"]),
        "profile": {
            "title": "Identity card",
            "what_it_is": "A scanned identity card of Sample Person Example, valid until 07 05 2031.",
            "language": "en", "doc_type": "id_document", "life_domains": ["legal_admin"],
            "dates": [{"label": "issued", "iso": "2021-05-07", "printed": "07 05 2021"},
                      {"label": "valid_until", "iso": "2031-05-07", "printed": "07 05 2031"},
                      # a model's misreading on purpose: the card prints 03 11 1990. Verification grades it
                      # `not_in_text` (§26.4), so this card scores under 90 % (the G3 value case, D36)
                      {"label": "other", "iso": "1990-11-08", "printed": "08 11 1990"}],
            "entities": [{"name": "Sample Person Example", "type": "person", "printed": "SAMPLE PERSON EXAMPLE",
                          "forms": []}],
            "key_values": [{"label": "document_number", "printed": "QX 4471902", "canonical": "QX4471902"}],
            "topics": ["identity card", "personal identification"],
            "topics_native": ["identity card", "personal identification"],
            "topic_words": ["identity card", "tarjeta de identidad", "посвідчення особи", "удостоверение личности"],
            "label_words": ["identity document", "identity documents", "documento de identidad",
                            "documentos de identidad", "документ, що посвідчує особу",
                            "документи, що посвідчують особу", "документ, удостоверяющий личность",
                            "документы, удостоверяющие личность"],
            "domain_words": ["legal", "administrative", "trámites", "documentos oficiales", "юридичні питання",
                             "документи", "юридические вопросы", "документы"],
            "validity_words": _VALIDITY_WORDS},
    },
    "Documents/Phone photos/IMG_20260702_091503.jpg": {
        "photo": {"verdict": "text", "photo": None},   # camera facet: capture is detected, not said (§26.3 rule 1)
        "reader": _outcome(METER_CARD_LINES, "form", "A meter reading card with a reading and a date.",
                           "2026-07-02T00:00:00+00:00", []),
        "profile": {
            "title": "Meter reading card", "what_it_is": "A meter reading card: meter GM-30517, reading 04412, "
                                                        "dated 02/07/2026.",
            "language": "en", "doc_type": "form", "life_domains": ["other"],
            "dates": [{"label": "event", "iso": "2026-07-02", "printed": "02/07/2026"}],
            "entities": [],
            "key_values": [{"label": "other_id", "printed": "GM-30517", "canonical": "GM30517"}],
            "topics": ["meter reading"], "topics_native": ["meter reading"],
            "topic_words": ["meter reading", "lectura del contador", "показання лічильника", "показания счётчика"],
            "label_words": ["form", "forms", "formulario", "formularios", "форма", "форми", "формы"],
            "domain_words": [], "validity_words": []},
    },
    "Screenshots/IMG_0412.png": {
        "photo": {"verdict": "text", "photo": None, "capture": "screenshot"},   # the model said it: keyed `cpa:`
        "reader": _outcome(LOAN_SCREEN_LINES, "other", "A library loan reminder.", "2026-09-24T00:00:00+00:00",
                           ["Example Library"]),
        "profile": {
            "title": "Library loan reminder",
            "what_it_is": "A loan reminder from Example Library for Atlas of Birds, to return by 24/09/2026.",
            "language": "en", "doc_type": "other", "life_domains": ["leisure"],
            "dates": [{"label": "due", "iso": "2026-09-24", "printed": "24/09/2026"}],
            "entities": [{"name": "Example Library", "type": "organization", "printed": "Example Library",
                          "forms": []}],
            "key_values": [],
            "topics": ["library loan", "book return"], "topics_native": ["library loan", "book return"],
            "topic_words": ["library loan", "préstamo de biblioteca", "позика бібліотеки", "заём библиотеки"],
            "label_words": [], "domain_words": ["leisure", "ocio", "дозвілля", "досуг"], "validity_words": []},
    },
    "Photos/Camera/IMG_20260814_204712.jpg": {
        "photo": {"verdict": "photo", "photo": {
            "description": "A large orange sun sinking behind dark hills under a purple and orange sky.",
            "objects": [{"name": "sun", "forms": _forms("sun", "sol", "сонце", "солнце")},
                        {"name": "hills", "forms": _forms("hills", "colinas", "пагорби", "холмы")}],
            "scene": "sunset over hills", "visible_text": "",
            "scene_labels": ["sunset", "golden_hour", "landscape"]}},
    },
    "Photos/Camera/IMG_20260720_131644.jpg": {
        "photo": {"verdict": "photo", "photo": {
            "description": "A wide sandy beach with calm blue sea under a clear sky.",
            "objects": [{"name": "sand", "forms": _forms("sand", "arena", "пісок", "песок")},
                        {"name": "waves", "forms": _forms("waves", "olas", "хвилі", "волны")}],
            "scene": "sandy beach by the sea", "visible_text": "",
            "scene_labels": ["beach", "sea", "sunny", "travel"]}},
    },
    "Photos/Camera/IMG_20260905_172209.jpg": {
        "photo": {"verdict": "photo", "photo": {
            "description": "A tall iron lattice tower rising above the roofs of a city, in Paris.",
            "objects": [{"name": "Eiffel Tower",
                         "forms": _forms("Eiffel Tower", "Torre Eiffel", "Ейфелева вежа", "Эйфелева башня")}],
            "scene": "city landmark", "visible_text": "",
            "scene_labels": ["architecture", "city_street", "travel"]}},
    },
}
