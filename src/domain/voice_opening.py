"""The call-opening note (SYSTEM_TURN_FINGERPRINT_RFC, voice delta): a call has no user turn and its history holds
summaries of earlier calls, so the main service builds this keyed note and the relay sends it as the first user item.
Pure: stdlib + domain."""
from typing import Any, Mapping

from src.domain.language import LanguageCode

# The session-config field that carries the keyed opening note from the main service to the relay.
VOICE_OPENING_FIELD = "opening"

_EN = (
    "The call has just connected and the caller is listening, waiting for you to open the conversation; they have "
    "not said anything yet. This note comes from the system, not from the caller. Silence at this moment is how "
    "every call starts, so it tells you nothing about the line or about them, and there is nothing to remark on. "
    "The conversation history also holds summaries of earlier calls: the system wrote them as a record, the caller "
    "did not, and how an earlier call went is not what this call is about, so do not open with it. Open the call "
    "the way you would with someone you know well: in one or two short sentences pick up something real, from what "
    "the caller has been busy with lately in the chat, the time of day or what you know of them, so that they "
    "have something to answer, rather than a bare hello."
)

_UK = (
    "Дзвінок щойно з'єднався, і співрозмовник слухає, чекаючи, що розмову почнеш ти; він ще нічого не сказав. Ця "
    "примітка від системи, а не від співрозмовника. Тиша в цю мить — так починається кожен дзвінок, тож вона нічого "
    "не каже ні про лінію, ні про нього, і коментувати тут нічого. В історії розмови є й підсумки попередніх "
    "дзвінків: їх написала система як запис, а не співрозмовник, і те, як минув попередній дзвінок, не те, про що "
    "цей дзвінок, тож не починай з нього. Почни так, як почав би з людиною, яку добре знаєш: одним-двома короткими "
    "реченнями зачепи щось справжнє — з того, що останнім часом займає співрозмовника в чаті, з пори дня чи "
    "з того, що ти про нього знаєш, — щоб йому було що відповісти, а не просто привітайся."
)

_FR = (
    "L'appel vient d'être établi et la personne qui appelle écoute, elle attend que tu engages la "
    "conversation ; elle n'a encore rien dit. Cette note vient du système, pas de la personne qui "
    "appelle. Le silence à ce moment-là, c'est ainsi que commence chaque appel : il ne t'apprend rien sur"
    " la ligne ni sur elle, et il n'y a rien à commenter. L'historique de la conversation contient aussi "
    "des résumés d'appels précédents : le système les a rédigés pour en garder une trace, pas la personne"
    " qui appelle, et la façon dont un appel précédent s'est passé n'est pas le sujet de cet appel, donc "
    "ne commence pas par là. Ouvre l'appel comme tu le ferais avec quelqu'un que tu connais bien : en une"
    " ou deux phrases courtes, reprends quelque chose de réel, tiré de ce qui a occupé la personne ces "
    "derniers temps dans le chat, du moment de la journée ou de ce que tu sais d'elle, afin qu'elle ait "
    "quelque chose à répondre, plutôt qu'un simple bonjour."
)

_ES = (
    "La llamada acaba de conectarse y la persona que llama está escuchando, esperando a que abras la "
    "conversación; todavía no ha dicho nada. Esta nota viene del sistema, no de la persona que llama. El silencio "
    "en este momento es como empieza cada llamada, así que no te dice nada sobre la línea ni sobre ella, y no hay "
    "nada que comentar. El historial de la conversación también contiene resúmenes de llamadas anteriores: el "
    "sistema los escribió como un registro, la persona que llama no, y cómo fue una llamada anterior no es el "
    "tema de esta llamada, así que no empieces por ahí. Abre la llamada como lo harías con alguien a quien "
    "conoces bien: en una o dos frases cortas retoma algo real, de lo que la persona ha tenido entre manos "
    "últimamente en el chat, de la hora del día o de lo que sabes de ella, para que tenga algo que responder, en "
    "lugar de un simple saludo."
)

_TEXTS = {LanguageCode.UK: _UK, LanguageCode.EN: _EN, LanguageCode.FR: _FR, LanguageCode.ES: _ES}

MIRROR_TAIL = (
    "Speak whichever language the caller used in the last message they wrote themselves in the "
    "conversation history; User lines that start with [System: are records the system wrote, not the caller's words. "
    "From their first words on, follow the language they speak."
)


# History entries the code wrote carry this prefix (call summaries, drive-note receipts): they are not the caller's words.
SYSTEM_RECORD_PREFIX = "[System:"


def is_caller_message(entry: Mapping[str, Any]) -> bool:
    """A user-role history entry the caller wrote, not a code-written `[System: …]` record."""
    return entry.get("role") == "user" and not str(entry.get("content", "")).strip().startswith(SYSTEM_RECORD_PREFIX)


def voice_opening_text(mirror: bool, language: LanguageCode, has_caller_message: bool) -> str:
    """Mirror mode: the English text, with `MIRROR_TAIL` only when the history holds a message of the caller's.
    Fixed mode: the text in `language`, no language sentence."""
    if mirror:
        return f"{_EN} {MIRROR_TAIL}" if has_caller_message else _EN
    return _TEXTS[language]
