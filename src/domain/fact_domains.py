"""
Fact Domain Registry
====================

Single source of truth for the 27 life areas a fact is filed under (fact profile RFC §3). Every reader takes keys,
labels, prompt rows and enums from here; no other module lists them.

Domain:
    Typed key constants. A key is a stored value (`primary_domain`, `domain_keys`): never renamed, only relabelled.

FactDomainEntry / FACT_DOMAINS:
    One declaration per domain, in the taxonomy's order: UI labels, the query prompt's LIFE AREAS row, the profiler's
    SUBJECT ENUM row, the use-area and voice flags, the Cabinet order, and the old `FactDomain` values the key replaces
    in stored configuration (never in facts).

Functions:
    Lookups, the enums derived from the entries, the prompt renderers and the legacy configuration map.

This module holds two public classes (`Domain` and `FactDomainEntry`) on purpose, as `agent_manifest.py` does: the
constants and the declarations that use them are one registry.

To add a domain: one constant, one entry. Its rows ship with the code; no token upload.
"""

import textwrap
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Optional


class Domain:
    """Typed constants for the registry keys."""
    BIOGRAPHY        = "biography"
    FAMILY           = "family"
    PEOPLE           = "people"
    PETS             = "pets"
    HEALTH           = "health"
    SPORT            = "sport"
    FOOD             = "food"
    MEDIA            = "media"
    HOBBIES          = "hobbies"
    GOING_OUT        = "going_out"
    MEMORIES         = "memories"
    LIFESTYLE        = "lifestyle"
    BELIEFS          = "beliefs"
    HOME             = "home"
    TRANSPORT        = "transport"
    TECH             = "tech"
    BELONGINGS       = "belongings"
    APPEARANCE       = "appearance"
    TRAVEL           = "travel"
    MONEY            = "money"
    SAVINGS          = "savings"
    STATE_LAW        = "state_law"
    WORK             = "work"
    PROJECTS         = "projects"
    LEARNING         = "learning"
    ABILITIES        = "abilities"
    AGENT_DIRECTIVES = "agent_directives"


@dataclass(frozen=True)
class FactDomainEntry:
    """One life area: its key and everything every reader needs about it."""
    key: str                                # Domain.* value, stored in primary_domain / domain_keys
    labels: Mapping[str, str]               # UI label per locale: en, uk, fr, es
    query_row: Optional[str]                # the LIFE AREAS row of the query prompt; None for agent_directives
    subject_row: Optional[str]              # the SUBJECT ENUM row of the profiler; None for flag-decided domains
    use_area: bool                          # member of the USE-AREA enum
    voice_open: bool                        # in Lelik's default list
    cabinet_order: int
    legacy_config_values: tuple[str, ...]   # old FactDomain values this key replaces in STORED CONFIGURATION only


def _labels(en: str, uk: str, fr: str, es: str) -> Mapping[str, str]:
    return MappingProxyType({"en": en, "uk": uk, "fr": fr, "es": es})


# Rows are the reviewed prompt texts byte for byte; a subject row keeps the taxonomy's own line breaks.
FACT_DOMAINS: tuple[FactDomainEntry, ...] = (
    FactDomainEntry(
        key=Domain.BIOGRAPHY,
        labels=_labels("Biography", "Біографія", "Biographie", "Biografía"),
        query_row=(
            "what identifies the user: name, birth date and birthplace, gender, citizenship status, native languages, "
            "country and city of residence, marital status, and who the user's spouse, partner, children, parents and "
            "siblings are"
        ),
        subject_row=None,
        use_area=False,
        voice_open=True,
        cabinet_order=1,
        legacy_config_values=("biographical", "location"),
    ),
    FactDomainEntry(
        key=Domain.FAMILY,
        labels=_labels("Family", "Родина", "Famille", "Familia"),
        query_row=(
            "relatives and everything about their lives: their traits, health, work, school, contact details, their "
            "own milestones, gifts between them and the user"
        ),
        subject_row=None,
        use_area=True,
        voice_open=False,
        cabinet_order=2,
        legacy_config_values=("network",),
    ),
    FactDomainEntry(
        key=Domain.PEOPLE,
        labels=_labels("People", "Люди", "Personnes", "Personas"),
        query_row=(
            "everyone outside the family and everything about their lives: friends, acquaintances, colleagues as "
            "persons, neighbours, contacts"
        ),
        subject_row=None,
        use_area=True,
        voice_open=False,
        cabinet_order=3,
        legacy_config_values=("network",),
    ),
    FactDomainEntry(
        key=Domain.PETS,
        labels=_labels("Pets", "Домашні тварини", "Animaux de compagnie", "Mascotas"),
        query_row="an animal kept as a pet, its health, vet, care, chip and passport",
        subject_row=None,
        use_area=True,
        voice_open=False,
        cabinet_order=4,
        legacy_config_values=(),
    ),
    FactDomainEntry(
        key=Domain.HEALTH,
        labels=_labels("Health & wellbeing", "Здоров'я та самопочуття", "Santé et bien-être", "Salud y bienestar"),
        query_row=(
            "the body and mind: conditions, symptoms, injuries, medications, doctors, tests, treatments, changing body "
            "measurements, allergies, mood, sleep problems, health insurance"
        ),
        subject_row=(
            "health and wellbeing: a condition, symptom, allergy, injury, medication, doctor, test, treatment, a body\n"
            "  measurement that changes over time, mood, emotion, loneliness, anxiety, sleep problem, tobacco, drugs, "
            "an alcohol\n"
            "  problem"
        ),
        use_area=True,
        voice_open=False,
        cabinet_order=5,
        legacy_config_values=("health", "medical_records"),
    ),
    FactDomainEntry(
        key=Domain.SPORT,
        labels=_labels("Sport", "Спорт", "Sport", "Deporte"),
        query_row="physical training and sport, gyms and memberships, sports gear, results at dated occasions",
        subject_row="physical training, a sport, gym, sports gear, a result at a dated or named sports occasion",
        use_area=True,
        voice_open=True,
        cabinet_order=6,
        legacy_config_values=("preference",),
    ),
    FactDomainEntry(
        key=Domain.FOOD,
        labels=_labels("Food & drinks", "Їжа та напої", "Nourriture et boissons", "Comida y bebidas"),
        query_row="food and any drink, diets, cuisines, restaurants and cafés, cooking, groceries",
        subject_row="food or any drink, a diet, a restaurant meal, cooking",
        use_area=True,
        voice_open=True,
        cabinet_order=7,
        legacy_config_values=("preference",),
    ),
    FactDomainEntry(
        key=Domain.MEDIA,
        labels=_labels("Film, books & music", "Фільми, книги та музика", "Films, livres et musique",
                       "Cine, libros y música"),
        query_row="films, series, books, music, podcasts and games, not at a live event and not a subscription",
        subject_row=(
            "a film, series, book, music, podcast or game, not at a live event and not a subscription to a service"
        ),
        use_area=True,
        voice_open=True,
        cabinet_order=8,
        legacy_config_values=("preference", "entertainment"),
    ),
    FactDomainEntry(
        key=Domain.HOBBIES,
        labels=_labels("Hobbies", "Хобі", "Loisirs", "Aficiones"),
        query_row="pastimes done for their own sake, with no stated deliverable, launch or sale",
        subject_row="a pastime done for its own sake, with no stated deliverable, launch or sale",
        use_area=True,
        voice_open=True,
        cabinet_order=9,
        legacy_config_values=("preference",),
    ),
    FactDomainEntry(
        key=Domain.GOING_OUT,
        labels=_labels("Celebrations & going out", "Свята та розваги", "Fêtes et sorties",
                       "Celebraciones y salidas"),
        query_row=(
            "parties, festive holidays and how they are celebrated, birthday parties, nightlife, festivals, concerts, "
            "theatre and other live events attended"
        ),
        subject_row=(
            "a party, festive holiday, celebration, night out, club or bar, festival or live event attended, and what\n"
            "  happened at it"
        ),
        use_area=True,
        voice_open=True,
        cabinet_order=10,
        legacy_config_values=("preference", "entertainment"),
    ),
    FactDomainEntry(
        key=Domain.MEMORIES,
        labels=_labels("Memories", "Спогади", "Souvenirs", "Recuerdos"),
        query_row="the past told for its own sake: childhood, youth, impressions, nostalgia, night dreams",
        subject_row=(
            "a recollection told for its own sake: childhood, youth, an impression or feeling about the past, "
            "nostalgia,\n"
            "  a night dream; not a past event stated as a dated fact"
        ),
        use_area=True,
        voice_open=True,
        cabinet_order=11,
        legacy_config_values=("preference",),
    ),
    FactDomainEntry(
        key=Domain.LIFESTYLE,
        labels=_labels("Lifestyle", "Спосіб життя", "Mode de vie", "Estilo de vida"),
        query_row=(
            "the shape of ordinary days: schedule, waking and sleeping, routines, recurring habits with no area of "
            "their own"
        ),
        subject_row=(
            "the shape of ordinary days: the daily schedule, waking and sleeping times, routines, and recurring "
            "personal\n"
            "  habits whose activity has no value above"
        ),
        use_area=True,
        voice_open=True,
        cabinet_order=12,
        legacy_config_values=("preference",),
    ),
    FactDomainEntry(
        key=Domain.BELIEFS,
        labels=_labels("Beliefs & community", "Переконання та спільнота", "Convictions et engagement",
                       "Creencias y comunidad"),
        query_row=(
            "beliefs, religious practice, political views, values, causes, volunteering, donating, associations and "
            "unpaid roles in them"
        ),
        subject_row=(
            "a belief, religious practice, political view, value, voting, volunteering, donating, membership of or an\n"
            "  unpaid role in an association or community body"
        ),
        use_area=True,
        voice_open=True,
        cabinet_order=13,
        legacy_config_values=("preference",),
    ),
    FactDomainEntry(
        key=Domain.HOME,
        labels=_labels("Home & housing", "Дім і житло", "Maison et logement", "Hogar y vivienda"),
        query_row=(
            "the dwelling and running it: postal address, rent, mortgage, landlord, utilities, repairs, furniture, "
            "appliances, garden, neighbourhood nuisances, home insurance"
        ),
        subject_row=(
            "the dwelling and running it: rent, mortgage, utilities, repairs, furniture, appliances, plants or garden, "
            "the\n"
            "  postal address, noise and other nuisances at home"
        ),
        use_area=True,
        voice_open=False,
        cabinet_order=14,
        legacy_config_values=("possession", "location"),
    ),
    FactDomainEntry(
        key=Domain.TRANSPORT,
        labels=_labels("Transport", "Транспорт", "Transport", "Transporte"),
        query_row=(
            "getting around in daily life: vehicles, maintenance, car insurance, parking, fuel, commuting, passes, "
            "taxis"
        ),
        subject_row="a vehicle, commuting or getting around in daily life",
        use_area=True,
        voice_open=False,
        cabinet_order=15,
        legacy_config_values=("possession",),
    ),
    FactDomainEntry(
        key=Domain.TECH,
        labels=_labels("Tech & telecom", "Техніка та зв'язок", "Technologie et télécoms",
                       "Tecnología y telecomunicaciones"),
        query_row=(
            "devices, telecom, internet, online services, apps and accounts and their subscriptions, the user's phone "
            "numbers and emails"
        ),
        subject_row=(
            "a device, telecom, internet, an online service, app or account, a subscription to an online or streaming\n"
            "  service or an app (the service is the subject), a phone number or email address"
        ),
        use_area=True,
        voice_open=False,
        cabinet_order=16,
        legacy_config_values=("possession", "communication"),
    ),
    FactDomainEntry(
        key=Domain.BELONGINGS,
        labels=_labels("Clothing & items", "Одяг і речі", "Vêtements et objets", "Ropa y objetos"),
        query_row="clothes, shoes, accessories, jewellery, bags and personal belongings",
        subject_row="clothes, shoes, hats, accessories, jewellery or personal belongings",
        use_area=True,
        voice_open=True,
        cabinet_order=17,
        legacy_config_values=("possession", "preference"),
    ),
    FactDomainEntry(
        key=Domain.APPEARANCE,
        labels=_labels("Appearance & care", "Зовнішність і догляд", "Apparence et soins", "Aspecto y cuidado"),
        query_row="tattoos, piercings, hair, haircut, beard, cosmetics, personal care",
        subject_row=(
            "what one does to one's looks: tattoos, piercings, hairstyle, haircut, beard, cosmetics, personal care"
        ),
        use_area=True,
        voice_open=True,
        cabinet_order=18,
        legacy_config_values=("preference",),
    ),
    FactDomainEntry(
        key=Domain.TRAVEL,
        labels=_labels("Travel", "Подорожі", "Voyages", "Viajes"),
        query_row=(
            "journeys away from daily life: trips, bookings, tickets, hotels, stays, travel insurance, travel habits"
        ),
        subject_row="a trip or journey away from daily life, its bookings and stays; not a permanent move",
        use_area=True,
        voice_open=False,
        cabinet_order=19,
        legacy_config_values=("location",),
    ),
    FactDomainEntry(
        key=Domain.MONEY,
        labels=_labels("Money", "Гроші", "Argent", "Dinero"),
        query_row=(
            "money flows and obligations: income, expenses, transfers, refunds, current accounts and balances, cards, "
            "loans, debts, budget, payments, taxes, inheritance, wills, life insurance"
        ),
        subject_row=(
            "money itself, not paid for a named thing or activity: income in general, a transfer or loan between "
            "people, a\n"
            "  current account or its balance, a card, a balance held for a named purpose, a budget, a tax, a state "
            "benefit,\n"
            "  inheritance, a will, life insurance, burial costs"
        ),
        use_area=True,
        voice_open=False,
        cabinet_order=20,
        legacy_config_values=("finance",),
    ),
    FactDomainEntry(
        key=Domain.SAVINGS,
        labels=_labels("Savings & investments", "Заощадження та інвестиції", "Épargne et placements",
                       "Ahorros e inversiones"),
        query_row="assets held: deposits, brokerage, stocks, funds, crypto, pension savings and their balances",
        subject_row="an asset held: a deposit, investment, crypto, pension savings or their balances",
        use_area=True,
        voice_open=False,
        cabinet_order=21,
        legacy_config_values=("finance",),
    ),
    FactDomainEntry(
        key=Domain.STATE_LAW,
        labels=_labels("State & law", "Держава і право", "État et droit", "Estado y ley"),
        query_row=(
            "dealings with the state and the law: passport, identity card, tax id, residency and permits, visas, "
            "citizenship and naturalisation, civil registry, courts, lawsuits, police, fines, certificates from "
            "authorities, state benefits, driving licence"
        ),
        subject_row=None,
        use_area=True,
        voice_open=False,
        cabinet_order=22,
        legacy_config_values=("legal",),
    ),
    FactDomainEntry(
        key=Domain.WORK,
        labels=_labels("Work", "Робота", "Travail", "Trabajo"),
        query_row=(
            "earning a living: job, employer, business, clients, career, salary, employment contract, colleagues' roles"
        ),
        subject_row=(
            "earning a living: a job, employer, business, clients, career, salary, a colleague's role,\n"
            "  military or police service as a job"
        ),
        use_area=True,
        voice_open=False,
        cabinet_order=23,
        legacy_config_values=("work",),
    ),
    FactDomainEntry(
        key=Domain.PROJECTS,
        labels=_labels("Projects", "Проєкти", "Projets", "Proyectos"),
        query_row="own projects with a stated deliverable, launch or sale, their tools and progress",
        subject_row="an own project with a stated deliverable, launch or sale",
        use_area=True,
        voice_open=False,
        cabinet_order=24,
        legacy_config_values=("project",),
    ),
    FactDomainEntry(
        key=Domain.LEARNING,
        labels=_labels("Learning", "Навчання", "Apprentissage", "Aprendizaje"),
        query_row=(
            "studying: schools, courses, degrees, diplomas, educational certificates, languages being studied, exams"
        ),
        subject_row=(
            "studying: a school, an ongoing course, a degree, an educational certificate, a language being studied, "
            "an\n"
            "  exam"
        ),
        use_area=True,
        voice_open=False,
        cabinet_order=25,
        legacy_config_values=("education",),
    ),
    FactDomainEntry(
        key=Domain.ABILITIES,
        labels=_labels("Abilities & character", "Здібності та характер", "Aptitudes et caractère",
                       "Habilidades y carácter"),
        query_row="what the user can and cannot do and how well, languages spoken, personality traits",
        subject_row=(
            "what someone can or cannot do and how well, a level or performance stated without a date or occasion,\n"
            "  languages spoken (native or learned), or what someone is like"
        ),
        use_area=True,
        voice_open=True,
        cabinet_order=26,
        legacy_config_values=("skill", "preference"),
    ),
    FactDomainEntry(
        key=Domain.AGENT_DIRECTIVES,
        labels=_labels("Agent directives", "Вказівки асистенту", "Consignes à l'assistant",
                       "Instrucciones al asistente"),
        query_row=None,
        subject_row=None,
        use_area=False,
        voice_open=False,
        cabinet_order=27,
        legacy_config_values=("agent_directive",),
    ),
)

# The profiler's answer when a flag (identity, pet, state/law, person, tax/fine) already says everything.
SUBJECT_NONE = "none"
_SUBJECT_NONE_ROW = (
    "no further matter; the fact's whole point is what user_identity, pet, state_or_law, about_person or tax_or_fine\n"
    "  already captures. Only when one of those is set."
)
# Lifestyle closes the subject enum: its row reads "no value above".
_SUBJECT_LAST = (Domain.LIFESTYLE,)
# What a use area with no subject row stands for, in the USE-AREA line.
_USE_AREA_GLOSS = MappingProxyType({
    Domain.FAMILY: "a relative",
    Domain.PEOPLE: "another person",
    Domain.PETS: "a pet",
    Domain.STATE_LAW: "the state or the law",
})
_PROMPT_WIDTH = 118
_DEFAULT_LOCALE = "en"

_BY_KEY: Mapping[str, FactDomainEntry] = MappingProxyType({e.key: e for e in FACT_DOMAINS})


# ── Lookups ──────────────────────────────────────────────────────────────────

def domain_entry(key: str) -> Optional[FactDomainEntry]:
    """The entry of a registry key; None for anything else."""
    return _BY_KEY.get(key)


def is_known(key: str) -> bool:
    return key in _BY_KEY


def all_keys() -> tuple[str, ...]:
    """Every registry key, in the taxonomy's order."""
    return tuple(e.key for e in FACT_DOMAINS)


def _subject_entries() -> tuple[FactDomainEntry, ...]:
    rows = [e for e in FACT_DOMAINS if e.subject_row is not None]
    return tuple([e for e in rows if e.key not in _SUBJECT_LAST] + [e for e in rows if e.key in _SUBJECT_LAST])


def subject_enum() -> tuple[str, ...]:
    """The profiler's subject values: every domain with a subject row, then `none`."""
    return tuple(e.key for e in _subject_entries()) + (SUBJECT_NONE,)


def use_area_enum() -> tuple[str, ...]:
    """The subject enum without `none`, then the flag-decided use areas in registry order."""
    subjects = tuple(e.key for e in _subject_entries() if e.use_area)
    return subjects + tuple(e.key for e in FACT_DOMAINS if e.use_area and e.subject_row is None)


def voice_open_default() -> tuple[str, ...]:
    """Lelik's default voice list, in registry order."""
    return tuple(e.key for e in FACT_DOMAINS if e.voice_open)


def cabinet_keys() -> tuple[str, ...]:
    """Every registry key in Cabinet order."""
    return tuple(e.key for e in sorted(FACT_DOMAINS, key=lambda e: e.cabinet_order))


# ── Renderers (byte-equal to the reviewed prompt texts) ──────────────────────

def render_subject_enum_block() -> str:
    """The profiler's SUBJECT ENUM block, ending with the `none` row."""
    rows = [f"{e.key}: {e.subject_row}" for e in _subject_entries()]
    return "\n".join(["SUBJECT ENUM:", *rows, f"{SUBJECT_NONE}: {_SUBJECT_NONE_ROW}"])


def render_use_area_line() -> str:
    """The profiler's USE-AREA ENUM line: the subject enum plus each flag-decided use area with its gloss."""
    extras = ", ".join(f"{e.key} ({_USE_AREA_GLOSS[e.key]})"
                       for e in FACT_DOMAINS if e.use_area and e.subject_row is None)
    text = f"USE-AREA ENUM: the subject enum without {SUBJECT_NONE}, plus {extras}."
    return textwrap.fill(text, width=_PROMPT_WIDTH, break_long_words=False, break_on_hyphens=False)


def render_life_areas_block() -> str:
    """The query prompt's LIFE AREAS block: one row per domain that has a query row, in registry order."""
    rows = [f"{e.key}: {e.query_row}" for e in FACT_DOMAINS if e.query_row is not None]
    return "\n".join(["LIFE AREAS", *rows])


# ── Labels ───────────────────────────────────────────────────────────────────

def label(key: str, locale: str) -> Optional[str]:
    """The UI label of a key in a locale, falling back to English; None for an unknown key."""
    entry = _BY_KEY.get(key)
    if entry is None:
        return None
    return entry.labels.get(locale) or entry.labels[_DEFAULT_LOCALE]


# ── Stored configuration (never facts) ───────────────────────────────────────

def config_key(value: str) -> tuple[str, ...]:
    """Registry keys for one stored configuration value: a registry key maps to itself, an old `FactDomain` value to
    the keys that replace it, anything else to nothing (the caller logs the drop)."""
    if value in _BY_KEY:
        return (value,)
    return tuple(e.key for e in FACT_DOMAINS if value in e.legacy_config_values)
