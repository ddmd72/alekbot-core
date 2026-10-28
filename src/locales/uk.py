"""
Ukrainian UI messages library.
Shared across all adapters (Slack, Telegram, Web, etc.)

Adapters can override specific messages if needed, but 99% will use these.
"""
from typing import Dict, List, Optional
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
        "Збираю думки докупи (вони розбігаються)",
        "Перебираю варіанти... більшість дурні, шукаю розумний",
        "Гортаю внутрішній довідник 'Як здаватися розумним'",
        "Роздмухую звивини до робочої температури",
        "Думаю так напружено, що чути мій вентилятор",
        "Раджуся з підсвідомістю... вона мовчить, як завжди",
        "Вимикаю режим 'ледащо', вмикаю режим 'геній'",
        "Викликаю музу... вона знову на перекурі",
        "Обчислюю відповідь у 47 паралельних всесвітах",
        "Струшую пил зі своїх алгоритмів",
        "Медитую над вашим запитом, як справжній монах",
        "Перезавантажую здоровий глузд... майже готово",
        "Шукаю відповідь, гідну вашого запитання",
        "Тримаю паузу для драматичного ефекту",
        "Ще секунда... я майже геній",
        "Прокручую сценарії, де я точно не помиляюсь"
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


def get_message(status_type: StatusType, overrides: Optional[Dict[str, List[str]]] = None) -> List[str]:
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
    UIMessage.SOURCES_HEADING.value: "Джерела:",
    UIMessage.CONSOLIDATION_STARTED.value: "🧠 Розпочато консолідацію пам'яті…",
    UIMessage.VOICE_REQUEST_FAILED.value: "Запит не вдалося виконати.",
    UIMessage.SKILL_USAGE.value: (
        "Навички: `$skill list` — ваші навички · `$skill save CODE` — зберегти чернетку · "
        "`$skill delete NAME` — видалити одну з ваших навичок"
    ),
    UIMessage.SKILL_SAVED.value: "✅ Навичку `{name}` збережено (v{version}). Вона доступна з наступного повідомлення.",
    UIMessage.SKILL_DRAFT_NOT_FOUND.value: "❌ Немає чернетки з кодом `{code}`. Попросіть мене скласти навичку ще раз.",
    UIMessage.SKILL_NOT_SAVED.value: "❌ Не збережено: {reason}",
    UIMessage.SKILL_LIST_HEADER.value: "Ваші навички:",
    UIMessage.SKILL_LIST_EMPTY.value: "У вас ще немає власних навичок.",
    UIMessage.SKILL_SYSTEM_HEADER.value: "Вбудовані:",
    UIMessage.SKILL_DELETED.value: "🗑️ Навичку `{name}` видалено.",
    UIMessage.SKILL_NOT_FOUND.value: "❌ Немає навички з назвою `{name}`.",
    UIMessage.SKILL_BUILT_IN.value: "`{name}` — вбудована навичка, її не можна видалити.",
    UIMessage.SKILL_UNAVAILABLE.value: "Навички зараз недоступні.",
    UIMessage.SKILL_PREVIEW_FILE_TITLE.value: "Чернетка навички: {name}",
    UIMessage.SKILL_PREVIEW_DELIVERY_FAILED.value: "⚠️ Чернетку навички не вдалося доставити. Попросіть мене скласти її ще раз.",
    UIMessage.SKILL_PREVIEW_REF_FILE_TITLE.value: "Чернетка навички: {name} — {path}",
    UIMessage.SKILL_LIST_FILES.value: "файлів: {count}",
    UIMessage.SKILL_CHANGE_NEW.value: "+ {path} (нове, {size})",
    UIMessage.SKILL_CHANGE_NEW_UPLOAD.value: '+ {path} ← "{source}" (ваш файл, {size})',
    UIMessage.SKILL_CHANGE_CHANGED.value: "~ {path} (змінено)",
    UIMessage.SKILL_CHANGE_CHANGED_UPLOAD.value: '~ {path} ← "{source}" (змінено)',
    UIMessage.SKILL_CHANGE_REMOVED.value: "− {path} (видалено)",
    UIMessage.SKILL_CHANGE_UNCHANGED.value: "= незмінені файли: {count}",
    UIMessage.MEMORY_PREVIEW_FILE_TITLE.value: "Факти для пам'яті: {count}",
    UIMessage.MEMORY_PREVIEW_HEADER.value: "🧠 Факти для довготривалої пам'яті ({count}) — у файлі вище; нічого не збережено, доки ви не вставите команду нижче.",
    UIMessage.MEMORY_PREVIEW_DELIVERY_FAILED.value: "⚠️ Не вдалося надіслати список фактів. Попросіть мене запропонувати їх знову.",
    UIMessage.MEMORY_USAGE.value: "Пам'ять: `$memory save КОД` — зберегти запропоновані мною факти",
    UIMessage.MEMORY_SAVED.value: "✅ Передано в пам'ять: {count}. Пошук знайде їх після фонової консолідації, зазвичай за кілька годин.",
    UIMessage.MEMORY_DRAFT_NOT_FOUND.value: "❌ Немає пропозиції з кодом `{code}`. Попросіть мене запропонувати факти знову.",
    UIMessage.MEMORY_UNAVAILABLE.value: "Збереження фактів зараз недоступне.",
    UIMessage.DRIVE_NOTES_USAGE.value: 'Нотатки до файлів: `$drive-notes erase-all` — назавжди стерти всі твої нотатки до файлів разом з історією (я надішлю код для підтвердження); встав `$drive-notes erase КОД` або `$drive-notes erase-all КОД`, щоб підтвердити',
    UIMessage.DRIVE_NOTES_UNAVAILABLE.value: 'Нотатки до файлів зараз недоступні.',
    UIMessage.DRIVE_NOTES_NONE.value: 'У тебе немає нотаток до файлів.',
    UIMessage.DRIVE_NOTES_NO_SUCH_REQUEST.value: '❌ Такого запиту на стирання немає: код неправильний, прострочений або вже використаний. Нічого не стерто.',
    UIMessage.LONG_TURN_NOTICE.value: "⏳ Працюю — це надовше. Відповім, коли закінчу; можна писати далі.",
    UIMessage.LATE_ANSWER_PREFIX.value: "[відкладена відповідь]",
    UIMessage.LONG_TURN_FAILED.value: "Не вдалося завершити — щось зламалося по дорозі.",
    UIMessage.LONG_TURN_CANCELLED.value: "Зупинив, як ти просив.",
    UIMessage.LONG_TURN_LOST.value: "Я втратив цю задачу посеред роботи (сервіс перезапустився). Спитай ще раз, будь ласка.",
    UIMessage.DRIVE_DELETED_FILE.value: "🗑️ Видалено з диска: {path}",
    UIMessage.DRIVE_DELETED_FOLDER.value: "🗑️ Видалено папку: {path} (файлів: {count})",
    UIMessage.DRIVE_REPLACED.value: "✏️ Замінено: {path} ({before} → {after}; попередня версія — в історії версій)",
    UIMessage.DRIVE_DELETE_FAILED.value: "⚠️ Видалення {path} не завершилося.",
    UIMessage.DRIVE_REPLACE_FAILED.value: "⚠️ Заміна {path} не завершилася.",
    UIMessage.DRIVE_MIRROR_WRITE_REFUSED.value: "Цю папку веде бот, і її файли перезаписуються; щоб зберегти власну версію, скопіюйте файл до іншої папки.",
    UIMessage.DRIVE_TRANSCRIPTS_README.value: (
        "# Розшифровки\n\n"
        "Цю папку веде бот: файли тут можуть бути переписані; не редагуйте їх. "
        "Бот перезаписує кожен файл тут, і в Archive/ теж, тож ваші зміни не збережуться. "
        "Щоб мати власну версію, скопіюйте файл до іншої папки. "
        "Розшифровки видалених записів лежать в Archive/; якщо видалити файл звідти, бот про нього забуде."
    ),
    UIMessage.DRIVE_TRANSCRIPT_OPEN_RECORDING.value: "відкрити запис",
    UIMessage.DRIVE_TRANSCRIPT_MANAGED_LINE.value: (
        "Цей файл веде бот: його може бути переписано. Автоматична розшифровка, можливі помилки."
    ),
    UIMessage.DRIVE_TRANSCRIPT_DURATION_MINUTES.value: "{n} хв",
    UIMessage.DRIVE_TRANSCRIPT_SPEAKERS_ONE.value: "{n} мовець",
    UIMessage.DRIVE_TRANSCRIPT_SPEAKERS_MANY.value: "мовців: {n}",
    UIMessage.DRIVE_TRANSCRIPT_STUB_NO_SPEECH.value: "У цьому записі не виявлено мовлення ({duration}).",
    UIMessage.DRIVE_TRANSCRIPT_STUB_TOO_LONG.value: "Цей запис задовгий для розшифровки ({duration}).",
    UIMessage.DRIVE_TRANSCRIPT_STUB_TOO_LARGE.value: "Цей запис завеликий для розшифровки.",
    UIMessage.DRIVE_TRANSCRIPT_STUB_NO_AUDIO.value: "У цьому записі немає звукової доріжки.",
    UIMessage.DEEP_RESEARCH_DRIVE_FOLDER.value: "Глибокі дослідження",
    UIMessage.DEEP_RESEARCH_DRIVE_COPIED.value: "📁 Скопійовано на твій диск: {path}",
    UIMessage.DEEP_RESEARCH_DRIVE_NOT_CONNECTED.value: "Не скопійовано на диск: диск не підключено.",
    UIMessage.DEEP_RESEARCH_DRIVE_COPY_FAILED.value: "Не скопійовано на диск: не вдалося зберегти.",
    UIMessage.DRIVE_INACCESSIBLE_NOTICE.value: (
        "⚠️ Файлів у вашій папці на диску, які бот не може відкрити через їхні налаштування спільного доступу: "
        "{count}. Щоб це виправити, скопіюйте кожен файл у нову папку, яку ви створите всередині папки бота: "
        "копії бот зможе прочитати. Список у вкладенні."
    ),
    UIMessage.DRIVE_INACCESSIBLE_LIST_TITLE.value: "Файли, які бот не може відкрити",
    UIMessage.MESSAGE_NOT_PROCESSED_BUSY.value: "⚠️ Не вдалося обробити твоє останнє повідомлення, бо я надто довго був зайнятий. Надішли його, будь ласка, ще раз.",
    UIMessage.MEMORY_REVIEW_QUESTION.value: "Це досі правда?",
    UIMessage.MEMORY_OPTION_STILL_TRUE.value: "Так",
    UIMessage.MEMORY_OPTION_ENDED.value: "Вже ні",
    UIMessage.MEMORY_OPTION_NEVER.value: "Ніколи",
    UIMessage.MEMORY_OPTION_DONT_KNOW.value: "Не пам'ятаю",
    UIMessage.MEMORY_REPLY_HINT.value: "Відповідай на це повідомлення цифрою або своїми словами.",
    UIMessage.MEMORY_ANSWER_NOTED.value: "Дякую, записано.",
    UIMessage.MEMORY_REPLY_NOTED.value: "Прийнято — я це врахую.",
    UIMessage.MEMORY_ALREADY_ANSWERED.value: "На це питання вже є відповідь, дякую.",
    UIMessage.MEMORY_FACT_CHANGED.value: "Цей факт тим часом змінився, тому відповідь не застосовано; я до нього ще повернуся.",
    UIMessage.MEMORY_QUESTION_CLOSED.value: "Це питання закрите.",
    UIMessage.MEMORY_DIGEST_HEADER.value: "Факти, які я відклав цього тижня:",
    UIMessage.MEMORY_DIGEST_FOOTER.value: "Щоб повернути один із них, напиши мені в чаті, що це досі так.",
    UIMessage.MEMORY_DIGEST_ARCHIVED.value: "більше не так",
    UIMessage.MEMORY_DIGEST_INVALIDATED.value: "ніколи не було так",
    UIMessage.MEMORY_REVIEW_QUESTION_HAPPENED.value: "Це відбулося?",
    UIMessage.MEMORY_REVIEW_QUESTION_RENEWED.value: "Це поновили?",
    UIMessage.MEMORY_REVIEW_QUESTION_PAID.value: "Це оплачено?",
    UIMessage.MEMORY_OPTION_HAPPENED.value: "Відбулося",
    UIMessage.MEMORY_OPTION_NOT_HAPPENED.value: "Не відбулося",
    UIMessage.MEMORY_OPTION_RENEWED.value: "Поновлено",
    UIMessage.MEMORY_OPTION_NOT_RENEWED.value: "Не поновлено",
    UIMessage.MEMORY_OPTION_NOT_NEEDED.value: "Вже не потрібно",
    UIMessage.MEMORY_OPTION_PAID.value: "Оплачено",
    UIMessage.MEMORY_OPTION_NOT_PAID.value: "Не оплачено",
    UIMessage.MEMORY_DIGEST_HISTORY_HEADER.value: "Записано як історію без твоєї відповіді:",
    UIMessage.MEMORY_DIGEST_INVOICES_HEADER.value: "Рахунки без підтвердження оплати:",
    UIMessage.MEMORY_DIGEST_INVOICES_CABINET.value: "Познач оплачені в кабінеті:",
    UIMessage.DRIVE_NOTE_SAVED.value: "📝 Нотатку збережено для {path} (версія {version}):",
    UIMessage.DRIVE_NOTE_UNCHANGED.value: "📝 Нотатка для {path} вже саме така (версія {version}); нічого не змінилося:",
    UIMessage.DRIVE_NOTE_CLEARED.value: "🗑️ Нотатку для {path} видалено. Її історію збережено, тож можна скасувати; скажи «зітри історію», щоб видалити її назавжди.",
    UIMessage.DRIVE_NOTE_NOTHING_TO_CLEAR.value: "Для {path} нотатки не було; нічого не змінилося.",
    UIMessage.DRIVE_NOTE_RESTORED.value: "↩️ Попередню нотатку для {path} повернуто (версія {version}):",
    UIMessage.DRIVE_NOTE_REDONE.value: "↪️ Скасовану зміну для {path} повернуто (версія {version}):",
    UIMessage.DRIVE_NOTE_RESTORED_EMPTY.value: "↩️ Скасовано для {path}: раніше нотатки не було, тож її видалено (версія {version}).",
    UIMessage.DRIVE_NOTE_UNDO_REPEATED.value: "↩️ Для {path} вже скасовано (версія {version}); нічого не змінилося:",
    UIMessage.DRIVE_NOTE_TYPE.value: "тип: {doc_type}",
    UIMessage.DRIVE_NOTE_AREAS.value: "сфери життя: {areas}",
    UIMessage.DRIVE_NOTE_UNDO_HINT.value: "Щоб скасувати, просто скажи.",
    UIMessage.DRIVE_NOTE_STATUS_SOON.value: "🔎 Пошук покаже її за кілька хвилин.",
    UIMessage.DRIVE_NOTE_STATUS_INDEXING_OFF.value: "🔎 Пошук по диску вимкнено, тож пошук покаже її, лише коли ти увімкнеш «Пошук на диску» в Кабінеті.",
    UIMessage.DRIVE_NOTE_STATUS_NOT_INDEXED.value: "🔎 Цього файлу немає в пошуковому індексі (він у папці, яку індекс тримає однією карткою, або ще не побачений), тож пошук нотатку не покаже.",
    UIMessage.DRIVE_NOTE_STATUS_NO_CARD.value: "🔎 Цей файл зараз не має картки в пошуку, тож пошук нотатку поки не покаже.",
    UIMessage.DRIVE_NOTE_STATUS_ARCHIVED.value: "🔎 Файлу вже немає на диску: цю нотатку знайде лише пошук серед видалених файлів.",
    UIMessage.DRIVE_NOTE_STATUS_OTHER_DRIVE.value: "🔎 Її диск не під'єднано, тож пошук не покаже цю нотатку, доки той диск знову не під'єднають.",
    UIMessage.DRIVE_NOTE_ERASE_REQUEST.value: "🗑️ Щоб назавжди стерти нотатку для {path} разом з усією історією (версій: {versions}), встав команду нижче протягом {minutes} хвилин. Доки ти цього не зробиш, нічого не стерто.",
    UIMessage.DRIVE_NOTE_ERASE_ALL_REQUEST.value: "🗑️ Щоб назавжди стерти всі твої нотатки до файлів (нотаток: {count}) разом з історією, встав команду нижче протягом {minutes} хвилин. Доки ти цього не зробиш, нічого не стерто.",
    UIMessage.DRIVE_NOTE_ERASED.value: "🗑️ Назавжди стерто нотаток разом з історією: {count}.",
    UIMessage.DRIVE_NOTE_ERASE_PARTIAL.value: "🗑️ Назавжди стерто нотаток: {erased}. Зараз не вдалося стерти: {left}; попроси мене стерти її ще раз.",
    UIMessage.DRIVE_NOTE_ERASE_ALL_PARTIAL.value: "🗑️ Назавжди стерто нотаток: {erased}. Зараз не вдалося стерти: {left}; надішли `$drive-notes erase-all` ще раз, щоб стерти їх.",
}
