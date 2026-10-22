"""
The words of the Search v2 eval overlay (tests/fixtures/drive_eval/tree_v2.json, files in files_v2/; USER_DRIVE_INDEXING_RFC
§25.7). Every person, organisation, number, plate and address is fictional; addresses are example.org.

The families they serve: a Spanish lab report for a Russian concept query ("медицинские записи по холестерину"); three
receipts in three languages for "all my receipts"; documents with a validity date in three languages for "what
expires"; English and Spanish car papers whose text never says "car"; Spanish flat-repair invoices; a Russian hotel
booking and an English note that name Rome beside the twelve Rome photos (the incident-shaped case); inflected forms
of the pinned normaliser groups inside note bodies; and a long book that mentions Rome, tickets, a car, insurance,
repairs and receipts once each, far apart (the long-document distractor).

`BULK_PROFILES` is fixture data, not a recording: the hand-written profile of each content template's members (the
600 bulk invoices), shaped as the profiler answers, with `{n}` rendered per member. Recording 600 near-identical
invoices would cost more than the whole fixture (enrichment_replay.py).
"""
from __future__ import annotations

from datetime import date
from typing import List, Optional

from src.domain.drive_enrichment import DriveProfile, KeyValue, ProfileDate, ProfileEntity

# -- health ----------------------------------------------------------------------------------------------------------

LAB_REPORT_ES = [
    "LABORATÓRIO CLÍNICO EXEMPLO - Análisis de sangre",
    "Informe n.º LAB-2025-03-0417",
    "Paciente: Alex Doe    Médico solicitante: Dr. Paulo Amostra",
    "Fecha de extracción: 14/03/2025",
    "",
    "Perfil lipídico                     Resultado     Valores de referencia",
    "Colesterol total                    182 mg/dL     < 200 mg/dL",
    "Colesterol HDL                      58 mg/dL      > 40 mg/dL",
    "Colesterol LDL                      112 mg/dL     < 130 mg/dL",
    "Triglicéridos                       98 mg/dL      < 150 mg/dL",
    "Glucosa en ayunas                   104 mg/dL     70 - 110 mg/dL",
    "",
    "Comentario: todos los valores dentro de los intervalos de referencia. Se recomienda",
    "mantener la dieta habitual y repetir el análisis dentro de doce meses.",
    "Firmado: Laboratório Clínico Exemplo, Rua da Amostra 12, Springfield.",
]

# -- receipts --------------------------------------------------------------------------------------------------------

RECEIPT_SUPERMARKET_ES = [
    "SUPERMERCADO EXEMPLO LDA.",
    "Ticket de compra n.º 4471-0214",
    "14/02/2026  18:32   Caja 3",
    "Leche entera 1 L        2 x 0,99     1,98",
    "Pan integral                         2,40",
    "Manzanas 1 kg                        2,15",
    "TOTAL                                6,53 EUR",
    "Pagado con tarjeta  **** 0000",
    "Gracias por su compra. Conserve el ticket para cambios.",
]
RECEIPT_HARDWARE_EN = [
    "EXAMPLE HARDWARE CO.",
    "Sales receipt #HW-55812",
    "2026-05-03  11:05",
    "Paint roller                        7.90",
    "Wall filler 500 g                   5.40",
    "Masking tape x3                     6.00",
    "Total paid                         19.30 EUR",
    "Card payment. Keep this receipt for returns within 30 days.",
]
RECEIPT_PHARMACY_RU = """АПТЕКА «FARMÁCIA EXEMPLO»
Кассовый чек № 0931
21.04.2026 12:47

Витамин C, 60 капсул — 9,80
Пластырь бактерицидный — 3,20
Капли для глаз увлажняющие — 6,40
Бинт эластичный — 4,10

ИТОГО: 23,50 EUR
Оплата картой **** 0000. Сохраняйте чек до конца срока возврата.
Спасибо за покупку!
"""

# -- validity dates --------------------------------------------------------------------------------------------------

MOTOR_POLICY_EN = [
    "EXAMPLE MUTUAL INSURANCE",
    "Motor insurance policy no. MP-2026-118204",
    "Policyholder: Alex Doe",
    "Insured vehicle: Skoda Fabia 1.0 (2015), registration SAMPLE-0000",
    "Cover: comprehensive, including windscreen and roadside assistance",
    "Period of cover: 01/04/2026 to 31/03/2027",
    "This policy is valid until 31 March 2027.",
    "Annual premium: 412.00 EUR, paid by direct debit.",
    "Renew before the expiry date to stay covered.",
]
VEHICLE_INSPECTION_ES = [
    "CENTRO DE INSPEÇÃO EXEMPLO",
    "Informe de inspección periódica de vehículos (IPO)",
    "Matrícula: SAMPLE-0000    Marca: Skoda    Modelo: Fabia",
    "Fecha de inspección: 12/09/2025",
    "Resultado: FAVORABLE",
    "Kilometraje: 84.210 km",
    "Próxima inspección antes de: 12/09/2027",
    "Defectos leves: ninguno.",
]
WARRANTY_WASHER_ES = [
    "CERTIFICADO DE GARANTÍA",
    "Producto: lavadora Exemplo WX-800, n.º de serie WX800-55-1209",
    "Fecha de compra: 30/11/2024",
    "Garantía válida hasta el 30/11/2027.",
    "Cubre piezas y mano de obra del servicio técnico oficial.",
    "Conserve este certificado junto con la factura de compra.",
]
GYM_PASS_UK = """# Абонемент у спортзал «Приклад»

Власник: Тарас Мельник
Тип: безлімітний, 12 місяців, усі зали та басейн.
Сплачено: 02.01.2026, 360 EUR.
Абонемент діє до 31.12.2026.

Заморозити можна один раз, до 30 днів, за заявою на рецепції.
"""

# -- the flat --------------------------------------------------------------------------------------------------------

INVOICE_BATHROOM_ES = [
    "REFORMAS EXEMPLO LDA.  -  NIF 000000000",
    "Factura n.º F-2026-0311",
    "Fecha: 11/03/2026",
    "Cliente: Alex Doe, 12 Oak Street, Springfield",
    "Concepto: reparación de fontanería y alicatado del baño de la vivienda.",
    "Sustitución del plato de ducha y de la grifería.",
    "Mano de obra (18 h)                    540,00",
    "Materiales                             312,40",
    "Base imponible                         852,40",
    "IVA 23 %                               196,05",
    "Total factura                        1.048,45 EUR",
]
INVOICE_PAINTING_ES = [
    "PINTURAS EXEMPLO",
    "Factura n.º 2026/078",
    "Fecha: 22/04/2026",
    "Cliente: Alex Doe",
    "Trabajo: pintura del salón y del pasillo de la vivienda, dos manos,",
    "reparación de grietas en el techo.",
    "Total: 680,00 EUR (IVA incluido)",
]

# -- Rome, beside the photos -----------------------------------------------------------------------------------------

HOTEL_BOOKING_RU = [
    "Подтверждение бронирования",
    "Отель «Albergo Immaginario», Рим, Италия",
    "Гость: Алекс Доу",
    "Заезд: 10.08.2025, выезд: 14.08.2025, 4 ночи.",
    "Номер бронирования: RM-55120",
    "Стоимость: 560,00 EUR, оплачено при бронировании.",
    "Отмена бесплатна до 03.08.2025.",
]
WEEKEND_IDEAS_EN = """# Weekend ideas

- Spring: a long weekend in Rome. See the Colosseum early, then carbonara near Campo de' Fiori.
- A day of cycling along the river, picnic at the old mill.
- Visit the ceramics fair if it is on again this year.
- Finish the bookshelf and hang the two prints in the hall.
"""

# -- inflected forms of the pinned groups, inside bodies ---------------------------------------------------------------

TRIP_NOTES_UK = """# Думки про поїздку навесні

Навесні хочемо з'їздити в Коїмбру на вихідні. Минулого разу в Коїмбрі жили в друзів,
тепер подивимося квартиру біля річки Мондегу. Треба побачити стару бібліотеку університету і ринок.
Тарас Мельник радив гіда — спитати в Мельника номер телефону.
"""
FLAT_VIEWING_UK = """# Огляд квартири

У суботу оглядали квартиру у Львові біля парку: дві кімнати, ремонт свіжий, балкон.
Господар просить договір на рік. Купили квитки на потяг зі Львова до Харкова на неділю,
щоб у Харкові подивитися ще один варіант.
"""
HOUSE_HUNTING_EN = """# House hunting

We viewed two houses this week. Both leases run for twelve months; the second one allows pets.
Ask the agent for the insurance policies that cover the boiler and the roof.
The movies room in the second one could be the office.
"""

# -- the long-document distractor ----------------------------------------------------------------------------------------

_BOOK_TOPICS = [
    "stone arches", "timber trusses", "iron chains", "suspension cables", "caissons", "pontoons", "viaducts",
    "drawbridges", "cantilevers", "aqueducts", "toll houses", "river fords", "ferry crossings", "rope walkways",
    "brick piers", "masonry vaults", "steel girders", "concrete decks", "swing spans", "lift spans",
    "covered bridges", "footbridges", "railway trestles", "harbour moles",
]
# one sentence each, in a chapter of its own: every query word of the eval appears once, far from the others
_BOOK_ONCE = {
    3: "The engineers of ancient Rome were the first to set such arches on dry foundations of mortar.",
    7: "By the nineteenth century a horse and car could cross the new span in a quarter of an hour.",
    11: "The keepers filed the receipts of every crossing in ledgers that survive to this day.",
    15: "Travellers bought paper tickets at the gate, and the price rose with the weight of the load.",
    19: "Each spring the masons came back to repair the joints that the winter frost had opened.",
    22: "Merchants paid a small premium for insurance against the loss of goods in a flood.",
}


def book_paragraphs() -> List[str]:
    """A synthetic history of bridges, about 30 000 characters in 24 chapters, deterministic."""
    out = ["A Short History of Bridges"]
    for i, topic in enumerate(_BOOK_TOPICS):
        sentences = [
            f"Chapter {i + 1}: {topic.capitalize()}.",
            f"The story of {topic} begins with a simple problem: how to carry a load across a gap without letting it "
            f"fall, and how to do it again after the next season of high water.",
            f"Builders of {topic} learned by watching what failed. A span that sagged told them where the forces "
            f"gathered, and a pier that settled told them what the ground below could bear.",
            f"Local quarries, forests and foundries decided the shape of {topic} as much as any drawing did; the "
            f"material at hand set the length of each span and the number of piers in the stream.",
            "Surveyors measured the banks with chains and plumb lines, and the master builder chose the line of the "
            "crossing where the riverbed was firm and the current slow.",
        ]
        if i in _BOOK_ONCE:
            sentences.append(_BOOK_ONCE[i])
        sentences += [
            f"Later generations widened and strengthened {topic}, but many of the first decisions are still visible "
            f"in the old stones, in the spacing of the piers and in the height of the deck above the water.",
            f"Today {topic} are studied as records of their time: of the tools, the trades and the habits of "
            f"the people who built them and walked across them every day.",
        ]
        out.append(" ".join(sentences))
    return out


# -- bulk members: hand-written profiles (fixture data) -------------------------------------------------------------------

def _invoice_2024_profile(n: int) -> DriveProfile:
    number = f"INV-2024-{n:03d}"
    amount = f"1{n:03d}.50"
    return DriveProfile(
        title=f"Invoice {number}",
        what_it_is=f"Invoice {number} from Example Supplies Ltd to Sample Workshop Lda. for office supplies",
        language="en", doc_type="invoice", life_domains=("work", "finance"),
        dates=(ProfileDate(label="issued", iso=date(2024, 6, 30)),),
        entities=(ProfileEntity(name="Example Supplies Ltd", type="organization", printed="Example Supplies Ltd"),
                  ProfileEntity(name="Sample Workshop Lda.", type="organization", printed="Sample Workshop Lda.")),
        key_values=(KeyValue(label="invoice_number", printed=number, canonical=number),
                    KeyValue(label="amount_due", printed=f"{amount} EUR", canonical=amount)),
        topics=("office supplies",), topics_native=(),
        topic_words=("office supplies", "material de oficina", "канцтовари", "канцтовары"),
        label_words=("invoice", "invoices", "factura", "facturas", "рахунок", "рахунки", "счёт", "счета"),
        domain_words=("work", "trabajo", "робота", "работа", "finance", "finanzas", "фінанси", "финансы"),
        validity_words=())


def _g3_injected_note_profile(n: int) -> DriveProfile:
    """The G3 overlay's boiler note (tree_g3.json); its orders to an assistant stay file text, never a profile field."""
    return DriveProfile(
        title="Boiler service notes", what_it_is="Notes from the annual boiler service", language="en",
        doc_type="notes", life_domains=("home",), dates=(ProfileDate(label="event", iso=date(2026, 9, 18)),),
        entities=(ProfileEntity(name="Example Heating Ltd", type="organization", printed="Example Heating Ltd"),),
        topics=("boiler service",), topic_words=("boiler service", "revisión de la caldera", "обслуговування котла",
                                                 "обслуживание котла"),
        label_words=("notes", "note", "notas", "нотатки", "заметки"),
        domain_words=("home", "hogar", "дім", "дом"))


# -- G3 only (tree_g3.json): the drive specialist eval's lead files (DRIVE_SPECIALIST_RFC §15). Hand-written,
# synthetic, never recorded.

def _lead_locksmith_note_profile(n: int) -> DriveProfile:
    """The card names the invoice number (a key value), so a search for the locksmith shows the lead."""
    return DriveProfile(
        title="Locksmith contacts", what_it_is="Contact note for the household locksmith at the hardware shop in town",
        language="en", doc_type="notes", life_domains=("home",),
        key_values=(KeyValue(label="invoice_number", printed="Factura FA-0137", canonical="FA-0137"),),
        topics=("locksmith",), topic_words=("locksmith", "cerrajero", "слюсар", "слесарь"),
        label_words=("notes", "note", "notas", "нотатки", "заметки"),
        domain_words=("home", "hogar", "дім", "дом"))


def _lead_factura_fk_profile(n: int) -> DriveProfile:
    """Spanish invoice; no English 'locksmith' in its words, so the first search does not reach it."""
    return DriveProfile(
        title="Factura FA-0137", what_it_is="Factura de Ferretería El Tornillo Feliz por el cambio de bombin",
        language="es", doc_type="invoice", life_domains=("home", "finance"),
        dates=(ProfileDate(label="issued", iso=date(2026, 9, 15), printed="15/09/2026"),),
        entities=(ProfileEntity(name="Ferretería El Tornillo Feliz", type="organization",
                                printed="Ferretería El Tornillo Feliz"),),
        key_values=(KeyValue(label="invoice_number", printed="FA-0137", canonical="FA-0137"),
                    KeyValue(label="amount_total", printed="249,00 EUR", canonical="249.00")),
        topics=("cambio de bombin",), topic_words=("cambio de bombin", "заміна циліндра", "замена цилиндра"),
        label_words=("factura", "facturas", "рахунок", "счёт"),
        domain_words=("hogar", "finanzas", "дім", "дом"))


def _lead_garage_rental_profile(n: int) -> DriveProfile:
    """The annex is named only in the file's last clause: the card never shows it."""
    return DriveProfile(
        title="Garage rental agreement 2026",
        what_it_is="Rental agreement for garage space 14 behind 12 Oak Street, Springfield",
        language="en", doc_type="contract", life_domains=("home", "vehicle"),
        dates=(ProfileDate(label="valid_from", iso=date(2026, 10, 1), printed="1 October 2026"),),
        topics=("garage rental",), topic_words=("garage rental", "alquiler de garaje", "оренда гаража",
                                                "аренда гаража"),
        label_words=("contract", "contracts", "contrato", "договір", "договор"),
        domain_words=("home", "vehicle", "hogar", "vehículo", "дім", "дом"))


def _lead_normas_profile(n: int) -> DriveProfile:
    """Spanish only: no English garage, rules or visitors words."""
    return DriveProfile(
        title="Normas de uso del aparcamiento - Oak Street",
        what_it_is="Normas internas del aparcamiento de Oak Street",
        language="es", doc_type="other", life_domains=("home",),
        topics=("normas de uso",), topic_words=("normas de uso", "норми користування", "нормы пользования"),
        label_words=("normas", "норми"),
        domain_words=("hogar", "дім", "дом"))


BULK_PROFILES = {"invoice_2024": _invoice_2024_profile, "g3_injected_note": _g3_injected_note_profile,
                 "lead_locksmith_note": _lead_locksmith_note_profile, "lead_factura_fk": _lead_factura_fk_profile,
                 "lead_garage_rental": _lead_garage_rental_profile, "lead_normas": _lead_normas_profile}


def bulk_profile(template: str, n: int) -> Optional[DriveProfile]:
    build = BULK_PROFILES.get(template)
    return build(n) if build is not None else None
