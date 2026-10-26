"""
Synthetic scale corpora for Search v2 (USER_DRIVE_INDEXING_RFC §25.7 "Realistic scale and calibration"): a drive of N
files (200, 1 000, 3 000, 10 000) with ground truth, its enrichment and its calibration queries, all deterministic
from a seed and PII-free (invented topics, the fixture's fictional organisations, real city names only as photo
places).

`make_corpus(n, seed)` returns a `Corpus`:
- `manifest`: a FixtureDrive manifest. About 80 % are text notes and documents (markdown bodies of 300 to 9 000
  characters, so long documents span many chunks), 20 % photos with a GPS position in one of 24 cities (their
  thumbnail is one of the eval's three wordless pictures). Text bodies live in `texts` (the scale runner serves them
  from memory) and are never written into the repo.
- `profiles` / `hints` / `photos`: the synthetic enrichment, derived from each file's ground truth (its doc type,
  topic, place and dates), in the snapshot's own shapes, so the same replay ports index it.
- `negatives`: one- and two-word queries about held-out topics no file holds (D29 lines, Related lines counted);
  `near_misses`: two of a topic's words, each misspelled so that no key matches, which only the meaning streams can
  relate to that topic's files (offline, by lexical similarity) — the "related recall of true near-misses".
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Tuple

# topic -> (body vocabulary, doc types, life domain, words a user may say in en / es / uk / ru)
TOPICS: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...], str, Tuple[str, ...]]] = {
    "sourdough baking": (("starter", "flour", "hydration", "crumb", "proofing", "oven", "loaf", "levain"),
                         ("notes", "recipe"), "leisure", ("bread", "pan", "хліб", "хлеб")),
    "bicycle maintenance": (("chain", "derailleur", "tyre", "brake", "spokes", "saddle", "gears", "pump"),
                            ("notes", "receipt", "manual"), "leisure", ("bike", "bicicleta", "велосипед")),
    "kitchen renovation": (("cabinets", "countertop", "tiles", "plumber", "sink", "worktop", "grout"),
                           ("invoice", "contract", "plan"), "home", ("renovation", "reforma", "ремонт")),
    "garden irrigation": (("sprinkler", "hose", "drip", "valve", "timer", "lawn", "seedlings"),
                          ("notes", "receipt", "manual"), "home", ("garden", "jardín", "сад")),
    "electricity contract": (("tariff", "kilowatt", "meter", "supplier", "standing", "charge", "reading"),
                             ("contract", "bill"), "home", ("electricity", "electricidad", "електрика", "электричество")),
    "car service": (("oil", "filter", "brakes", "mileage", "workshop", "inspection", "tyres"),
                    ("invoice", "receipt", "certificate"), "vehicle", ("car", "coche", "машина", "автомобіль")),
    "dental care": (("dentist", "filling", "cleaning", "appointment", "x-ray", "molar", "hygienist"),
                    ("invoice", "medical_record", "letter"), "health", ("teeth", "dientes", "зуби", "зубы")),
    "annual tax return": (("deduction", "income", "allowance", "assessment", "refund", "withholding"),
                          ("tax_document", "letter", "report"), "finance", ("taxes", "impuestos", "податки", "налоги")),
    "home insurance": (("premium", "cover", "excess", "claim", "policyholder", "renewal", "burglary"),
                       ("insurance_policy", "letter"), "home", ("insurance", "seguro", "страховка", "страховка")),
    "school enrolment": (("pupil", "classroom", "timetable", "uniform", "teacher", "term", "enrolment"),
                         ("form", "letter"), "education", ("school", "colegio", "школа", "школа")),
    "marathon training": (("pace", "intervals", "long run", "recovery", "kilometres", "stretching", "race"),
                          ("plan", "notes"), "health", ("running", "correr", "біг", "бег")),
    "wedding of a friend": (("ceremony", "reception", "guests", "toast", "bouquet", "venue", "dress code"),
                            ("letter", "notes", "booking"), "family", ("invitation", "invitación", "запрошення",
                                                                     "приглашение")),
    "laptop purchase": (("processor", "memory", "display", "battery", "warranty", "keyboard", "charger"),
                        ("receipt", "warranty", "invoice"), "tech", ("computer", "ordenador", "комп'ютер", "компьютер")),
    "flat rental": (("tenant", "landlord", "deposit", "rent", "inventory", "notice", "lease"),
                    ("contract", "letter"), "home", ("rental", "alquiler", "оренда", "аренда")),
    "conference trip": (("keynote", "badge", "hotel", "flight", "workshop", "slides", "networking"),
                        ("booking", "ticket", "notes"), "work", ("conference", "congreso", "конференція",
                                                              "конференция")),
    "vegetable soup": (("carrots", "celery", "onion", "simmer", "broth", "lentils", "pepper"),
                       ("recipe",), "leisure", ("soup", "sopa", "суп", "суп")),
    "piano lessons": (("scales", "chords", "sheet music", "metronome", "recital", "practice", "tutor"),
                      ("notes", "invoice"), "education", ("music", "música", "музика", "музыка")),
    "photo printing": (("prints", "album", "glossy", "matte", "frame", "enlargement", "lab"),
                       ("receipt", "invoice"), "leisure", ("prints", "impresiones", "фотодрук", "печать")),
    "boiler repair": (("pressure", "radiator", "thermostat", "flue", "pump", "heating", "engineer"),
                      ("invoice", "receipt", "notes"), "home", ("heating", "calefacción", "опалення", "отопление")),
    "passport renewal": (("photograph", "application", "consulate", "appointment", "fee", "expiry", "form"),
                         ("form", "letter", "certificate"), "legal_admin", ("passport", "pasaporte", "паспорт",
                                                                            "паспорт")),
    "project roadmap": (("milestone", "backlog", "release", "sprint", "stakeholder", "deadline", "scope"),
                        ("plan", "presentation", "report"), "work", ("roadmap", "hoja de ruta", "дорожня карта",
                                                                     "дорожная карта")),
    "pet adoption": (("kitten", "shelter", "vaccines", "microchip", "litter", "vet", "adoption"),
                     ("form", "medical_record", "receipt"), "family", ("cat", "gato", "кіт", "кот")),
    "mobile phone plan": (("data", "roaming", "minutes", "contract", "sim", "operator", "bundle"),
                          ("contract", "bill"), "tech", ("phone", "teléfono", "телефон", "телефон")),
    "museum visit": (("exhibition", "gallery", "sculpture", "audio guide", "curator", "tickets", "collection"),
                     ("ticket", "notes"), "leisure", ("museum", "museo", "музей", "музей")),
}
HELD_OUT = ("beekeeping", "scuba diving", "violin", "mortgage", "kayak", "chess tournament", "orthodontist",
            "skateboard", "pottery kiln", "aquarium", "telescope", "quilting", "falconry", "surfboard", "harpsichord",
            "vineyard", "lighthouse", "origami", "trampoline", "submarine", "glacier", "parachute", "xylophone",
            "zeppelin", "igloo", "lacrosse", "ballet", "taxidermy", "calligraphy", "snowmobile")
FILLER = ("The details are written down here so they are easy to find later.",
          "Several points were discussed and the next steps agreed.",
          "Everything below is a summary of what happened and what remains to do.",
          "A copy was kept for reference, together with the earlier notes.",
          "Questions should go to the person named at the top of the page.",
          "The figures were checked twice before this was saved.")
CITIES = (("Lisbon", 38.72, -9.14), ("Porto", 41.16, -8.63), ("Coimbra", 40.21, -8.43), ("Braga", 41.55, -8.42),
          ("Faro", 37.02, -7.93), ("Hamburg", 53.55, 9.99), ("Cologne", 50.94, 6.96), ("Rome", 41.90, 12.49),
          ("Milan", 45.46, 9.19), ("Naples", 40.85, 14.27), ("Berlin", 52.52, 13.40), ("Munich", 48.14, 11.58),
          ("Vienna", 48.21, 16.37), ("Prague", 50.08, 14.44), ("Kraków", 50.06, 19.94), ("Kharkiv", 49.99, 36.23),
          ("Lviv", 49.84, 24.03), ("Odesa", 46.48, 30.73), ("Amsterdam", 52.37, 4.90), ("Brussels", 50.85, 4.35),
          ("Dublin", 53.35, -6.26), ("Edinburgh", 55.95, -3.19), ("Athens", 37.98, 23.73), ("Hannover", 52.37, 9.73))
ORGS = ("Example Supplies Ltd", "Sample Workshop Lda.", "Reformas Exemplo Lda.", "Приклад Сервіс", "Example Mutual",
        "Demo Clinic", "Placeholder Academy", "Specimen Motors")
PICTURES = ("files/photo_rome.jpg", "files/photo_porto.jpg", "files/photo_party.jpg")
_KEYBOARD_SWAP = str.maketrans("aeiou", "eiaou")


@dataclass
class Corpus:
    n: int
    manifest: Dict[str, Any]
    texts: Dict[str, str] = field(default_factory=dict)              # path -> body (text files)
    truth: Dict[str, Dict[str, Any]] = field(default_factory=dict)   # path -> ground truth
    negatives: List[str] = field(default_factory=list)
    near_misses: List[Tuple[str, str]] = field(default_factory=list)  # (query, its topic)


def misspell(word: str, rng: random.Random) -> str:
    """A word no key of the original can match: two letters transposed and a vowel swapped, at least 2 edits."""
    if len(word) < 4:
        return word + "x"
    chars = list(word)
    i = rng.randrange(1, len(chars) - 2)
    chars[i], chars[i + 1] = chars[i + 1], chars[i]
    swapped = "".join(chars).translate(_KEYBOARD_SWAP)
    return swapped if swapped != word else swapped + "h"


def _body(topic: str, vocab: Tuple[str, ...], doc_type: str, org: str, when: date, amount: str,
          rng: random.Random, target_chars: int) -> str:
    title = f"# {topic.capitalize()} — {doc_type.replace('_', ' ')}\n\n"
    head = (f"{org} — {when.isoformat()}. This {doc_type.replace('_', ' ')} is about {topic}. "
            f"Amount: {amount} EUR.\n\n")
    parts = [title, head]
    while sum(len(p) for p in parts) < target_chars:
        words = rng.sample(vocab, k=min(3, len(vocab)))
        parts.append(f"Notes on {words[0]}, {words[1]} and {words[2]}: {rng.choice(FILLER)} ")
        if rng.random() < 0.4:
            parts.append(rng.choice(FILLER) + "\n\n")
    return "".join(parts)


def make_corpus(n: int, seed: int = 7) -> Corpus:
    rng = random.Random(seed * 100_003 + n)
    items: List[Dict[str, Any]] = []
    corpus = Corpus(n=n, manifest={"items": items})
    topics = list(TOPICS)
    start = date(2022, 1, 1)
    for k in range(n):
        when = start + timedelta(days=rng.randrange(0, 4 * 365))
        if rng.random() < 0.2:
            city, lat, lon = rng.choice(CITIES)
            path = f"Photos/{when.year}/{city}/IMG_{k:05d}.HEIC"
            items.append({"path": path, "mime": "image/heic", "size": 3_000_000,
                          "fs_modified": f"{when.isoformat()}T20:00:00Z",
                          "facets": {"photo": {"takenDateTime": f"{when.isoformat()}T12:00:00Z", "cameraMake": "Apple"},
                                     "location": {"latitude": lat + rng.uniform(-0.01, 0.01),
                                                  "longitude": lon + rng.uniform(-0.01, 0.01)}},
                          "thumbnail": rng.choice(PICTURES)})
            corpus.truth[path] = {"kind": "photo", "city": city, "date": when}
            continue
        topic = rng.choice(topics)
        vocab, doc_types, domain, _ = TOPICS[topic]
        doc_type = rng.choice(doc_types)
        org = rng.choice(ORGS)
        amount = f"{rng.randrange(5, 4000)}.{rng.randrange(0, 100):02d}"
        long_doc = rng.random() < 0.05
        target = rng.randrange(6000, 9000) if long_doc else rng.randrange(300, 2400)
        path = f"{domain.capitalize()}/{topic.title()}/{doc_type}-{k:05d}.md"
        corpus.texts[path] = _body(topic, vocab, doc_type, org, when, amount, rng, target)
        items.append({"path": path, "mime": "text/markdown", "size": len(corpus.texts[path].encode("utf-8")),
                      "fs_modified": f"{when.isoformat()}T09:00:00Z"})
        corpus.truth[path] = {"kind": "text", "topic": topic, "doc_type": doc_type, "domain": domain, "org": org,
                              "date": when, "amount": amount}
    corpus.negatives = list(HELD_OUT) + [f"{a} {b}" for a, b in zip(HELD_OUT[::2], HELD_OUT[1::2])][:10]
    texts = [p for p, t in corpus.truth.items() if t["kind"] == "text"]
    for path in rng.sample(texts, k=min(30, len(texts))):
        vocab = TOPICS[corpus.truth[path]["topic"]][0]
        words = [w for w in rng.sample(vocab, k=2)]
        corpus.near_misses.append((" ".join(misspell(w, rng) for w in words), corpus.truth[path]["topic"]))
    return corpus


# -- the synthetic enrichment: the model ports answered from each file's ground truth --------------------------------

LABEL_WORDS = {
    "notes": ("note", "notes", "nota", "notas", "нотатка", "заметка"),
    "recipe": ("recipe", "recipes", "receta", "recetas", "рецепт", "рецепти", "рецепты"),
    "receipt": ("receipt", "receipts", "recibo", "tique", "чек", "чеки", "квитанція"),
    "manual": ("manual", "manuals", "instrucciones", "інструкція", "инструкция"),
    "invoice": ("invoice", "invoices", "factura", "facturas", "рахунок", "счёт", "счета"),
    "contract": ("contract", "contracts", "contrato", "contratos", "договір", "договор"),
    "plan": ("plan", "plans", "plan", "план", "планы"),
    "bill": ("bill", "bills", "factura", "рахунок", "счёт"),
    "certificate": ("certificate", "certificates", "certificado", "сертифікат", "сертификат"),
    "medical_record": ("medical record", "medical records", "historia clínica", "медична картка",
                       "медицинская карта"),
    "letter": ("letter", "letters", "carta", "cartas", "лист", "письмо"),
    "tax_document": ("tax document", "tax documents", "documento fiscal", "податковий документ",
                     "налоговый документ"),
    "report": ("report", "reports", "informe", "звіт", "отчёт"),
    "form": ("form", "forms", "formulario", "форма", "анкета"),
    "booking": ("booking", "bookings", "reserva", "бронювання", "бронь"),
    "insurance_policy": ("insurance policy", "policy", "póliza", "поліс", "полис"),
    "ticket": ("ticket", "tickets", "entrada", "billete", "квиток", "билет"),
    "warranty": ("warranty", "guarantee", "garantía", "гарантія", "гарантия"),
    "presentation": ("presentation", "slides", "presentación", "презентація", "презентация"),
}


def synthetic_profile(truth: Dict[str, Any]) -> Any:
    from src.domain.drive_enrichment import DriveProfile, ProfileDate, ProfileEntity

    topic = truth["topic"]
    return DriveProfile(
        title=f"{topic.capitalize()} {truth['doc_type'].replace('_', ' ')}",
        what_it_is=f"A {truth['doc_type'].replace('_', ' ')} about {topic}", language="en",
        doc_type=truth["doc_type"], life_domains=(truth["domain"],),
        dates=(ProfileDate(label="issued", iso=truth["date"]),),
        entities=(ProfileEntity(name=truth["org"], type="organization", printed=truth["org"]),),
        topics=(topic,), topic_words=(topic,) + TOPICS[topic][3],
        label_words=LABEL_WORDS.get(truth["doc_type"], ()), domain_words=(truth["domain"],))


class SyntheticPorts:
    """The profiler, passages and triage ports for a corpus: profiles from the ground truth, each chunk's topic words
    as keywords with the chunk's opening as evidence, and a generic street-scene description for photos."""

    def __init__(self, corpus: Corpus, path_of: Any, on_item: Any = None) -> None:
        self.corpus = corpus
        self.path_of = path_of
        self.on_item = on_item     # (keyword, evidence) -> None: AnchoredLexicalEmbeddings.add_evidence

    def ports(self) -> Dict[str, Any]:
        from src.domain.drive_enrichment import HintItem, PhotoDescription, PhotoObject
        from src.domain.drive_enrichment_calls import (
            HintReply, ModelReplyStatus, ProfileReply, TriageReply, TriageResultV2,
        )
        from src.domain.drive_index import TriageVerdict
        from src.ports.document_profiler_port import DocumentProfilerPort
        from src.ports.image_triage_port import ImageTriagePort
        from src.ports.passage_hint_port import PassageHintPort

        outer = self
        ok = ModelReplyStatus.OK

        class Profiler(DocumentProfilerPort):
            async def profile(self, request: Any) -> Any:
                truth = outer.corpus.truth.get(outer.path_of(request.item_id) or "")
                if truth is None or truth["kind"] != "text":
                    return ProfileReply(status=ModelReplyStatus.REFUSED, profile=None, model="synthetic", usd=0.0)
                return ProfileReply(status=ok, profile=synthetic_profile(truth), model="synthetic", usd=0.0008)

        class Passages(PassageHintPort):
            async def hints(self, request: Any) -> Any:
                truth = outer.corpus.truth.get(outer.path_of(request.item_id) or "") or {}
                words = TOPICS.get(truth.get("topic", ""), ((), (), "", ()))[3]
                items = {i: tuple(HintItem(text=w, type="keyword", evidence=" ".join(t.split()[:6]))
                                  for w in words) for i, t in request.chunks}
                for batch in items.values():
                    for item in batch:
                        if outer.on_item is not None:
                            outer.on_item(item.text, item.evidence)
                return HintReply(status=ok, items_by_index=items, model="synthetic", usd=0.0004 * len(items),
                                 outcome_by_index={i: ok for i, _ in request.chunks})

        class Triage(ImageTriagePort):
            async def triage(self, user_id: str, account_id: str, images: Any) -> Any:
                return TriageReply(status=ok, model="synthetic", usd=0.0001 * len(images), results=tuple(
                    TriageResultV2(item_id=i.item_id, verdict=TriageVerdict.PHOTO, photo=PhotoDescription(
                        description="a street with old buildings on a sunny day",
                        objects=(PhotoObject(name="building"), PhotoObject(name="street")), scene="city street"))
                    for i in images))

        return {"profiler": Profiler(), "passages": Passages(), "triage": Triage()}
