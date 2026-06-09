"""
Ukrainian UI messages library.
Shared across all adapters (Slack, Telegram, Web, etc.)

Adapters can override specific messages if needed, but 99% will use these.
"""
from typing import Dict, List
from ..domain.ui_messages import StatusType, UIMessage


# Українські повідомлення (центральна бібліотека)
ENTERTAINMENT_INTROS: List[str] = [
    "Розважся, поки я риюсь у мережі",
    "Поки я шукаю — тримай паузу з іронією",
    "Займи мозок, поки мій шукає",
    "Перерва на факт, поки я гуглю",
    "Розслабся, це пошук, а не політ на Марс",
    "Тримай коротку історію, поки я в дорозі",
    "Поки я нишпорю — тобі міні-десерт",
]

# Fallback prompts when user sends file without text
FILE_FALLBACK_IMAGE = "Що на цьому фото?"
FILE_FALLBACK_VIDEO = "Що у цьому відео?"
FILE_FALLBACK_PDF = "Розкажи про цей документ"
FILE_FALLBACK_DOCUMENT = "Що у цьому файлі?"
FILE_FALLBACK_GENERIC = "Подивись на цей файл"

UK_MESSAGES: Dict[str, List[str]] = {
    StatusType.THINKING.value: [
        "Розмірковую над сенсом буття... і вашим запитом",
        "Синхронізую нейрони... зачекайте, це боляче",
        "Занурююсь у глибини власного розуму",
        "Будую логічні ланцюжки... сподіваюсь, вони не розірвуться",
        "Активую когнітивні модулі на повну потужність",
        "Намагаюся не перегрітися від ваших геніальних ідей",
        "Консультуюся з ядром моєї особистості",
        "Збираю думки докупи (вони розбігаються)"
    ],
    StatusType.SEARCHING_MEMORY.value: [
        "Гортаю ваші архіви... десь тут це було",
        "Шукаю у вашій пам'яті... сподіваюсь, там прибрано",
        "Запитую у свого внутрішнього бібліотекаря",
        "Ниряю в океан ваших фактів",
        "Витягую спогади з найтемніших куточків",
        "Проводжу інвентаризацію ваших знань",
        "Шукаю голку в стозі вашої пам'яті"
    ],
    StatusType.SEARCHING_WEB.value: [
        "Виходжу в дикий інтернет... тримайте за мене кулаки",
        "Гуглю так, ніби від цього залежить моє життя",
        "Шукаю відповіді у всесвітній павутині",
        "Питаю у розумних людей в мережі",
        "Сканую цифрові горизонти",
        "Продираюся крізь нетрі інформаційного шуму",
        "Полюю на свіжі факти в мережі"
    ],
    StatusType.PROCESSING_FILE.value: [
        "Аналізую ваші файли",
        "Розбираю документ на атоми",
        "Вивчаю ваші вкладення",
    ],
    StatusType.ERROR.value: [
        "Ой! Мої нейрони переплуталися",
        "Щось пішло не так... мабуть, ретроградний Меркурій",
        "Сталася помилка, але я виправлюсь (колись)",
        "Мій внутрішній процесор каже 'ой'",
        "Здається, я перемудрив",
        "Система впала в екзистенційну кризу",
        "Помилка 404: Мій мозок не знайдено"
    ]
}


def get_entertainment_intros() -> List[str]:
    """Get entertainment intro phrases for web search."""
    return ENTERTAINMENT_INTROS


def get_message(status_type: StatusType, overrides: Dict[str, List[str]] = None) -> List[str]:
    """
    Get Ukrainian message for status type.
    
    Args:
        status_type: Type of status message
        overrides: Optional dict to override specific status types
        
    Returns:
        List of message variants
        
    Example:
        # Use default Ukrainian
        messages = get_message(StatusType.THINKING)
        
        # Override specific type
        messages = get_message(
            StatusType.THINKING,
            overrides={StatusType.THINKING.value: ["Custom думаю..."]}
        )
    """
    if overrides and status_type.value in overrides:
        return overrides[status_type.value]
    
    return UK_MESSAGES.get(status_type.value, ["Обробка..."])
# Fixed single-string UI messages (see domain.ui_messages.UIMessage)
UI_STRINGS: Dict[str, str] = {
    UIMessage.RESPONSE_READY.value: "✅ Відповідь готова.",
    UIMessage.RESPONSE_TRUNCATED_SUFFIX.value: "\n\n... (занадто довга відповідь)",
    UIMessage.EMPTY_MODEL_RESPONSE.value: "*(порожня відповідь від моделі)*",
    UIMessage.UNKNOWN_COMMAND.value: "Невідома команда: `{command}`",
    UIMessage.NEW_TOPIC_ACK.value: "Нова тема. Історію очищено.",
}
