# RFC: Task Complexity Classification & Dynamic Execution Settings

**Status**: Draft / thinking
**Owner**: Dmytro
**Date**: 2026-04-14

---

## 1. Motivation

Сейчас у оркестраторов есть две проблемы, связанные одной сутью:

1. **Роутер недорабатывает**. Классификация `complexity 1–10 → Quick/Smart` слишком крупная и не использует потенциал LLM-триажа. Мы платим за роутер на каждом сообщении, но получаем от него бинарный switch.
2. **Quick выродился в копию Smart**. После parity-рефакторинга отличия — только отсутствие re-evaluation после tool results и disabled intent_remap. Отдельный агент не оправдан.

**Идея**: роутер классифицирует вид задачи (семантически), а execution конфигурация (tier, thinking_effort, [опционально] provider) резолвится через таблицу, у которой есть дефолт в коде и user-level override. Smart становится единственным оркестратором, исполняющим с динамически подобранной конфигурацией. Quick — deprecated, удаляется отдельным PR.

Ключевое разделение ответственности:
- **Роутер** — понимает *вид задачи*, не знает об инфраструктуре.
- **Complexity settings table** — мапит вид → исполнительные параметры.
- **Smart** — потребляет resolved settings per-call.

## 2. Current state: entry points into Smart

Это критично для дизайна, потому что роутер сейчас покрывает только один из путей. Другие entry points либо задают параметры явно, либо идут на дефолтных.

| Entry point | Source | Проходит через Router сейчас? | Как задан execution контекст сейчас |
|---|---|---|---|
| User message (chat) | Slack / Telegram adapter → ConversationHandler | **Да** | Router outputs `target_agent` (quick/smart) |
| Reminder fire | Cloud Scheduler → WorkerHandler.fire_due_reminders → RemindersService → UserNotificationService.notify | **Нет** | Хардкод на Smart, `thinking_effort` не задан |
| Daily email review | Cloud Scheduler → WorkerHandler._handle_daily_email_review → notify | **Нет** | Хардкод на Smart + `thinking_effort="medium"` |
| Deep research result | Cloud Run Job → webhook/polling → notify | **Нет** | Хардкод на Smart |
| Async doc/PDF delivery | AgentWorkerHandler → notify | **Нет** | Хардкод на Smart |
| Agent → Agent delegation | DocPlanner → DocGenerator (coordinator) | **Нет** (роутера нет в цепочке) | Target агент фиксирован в coordinator; у каждого свой tier из AgentContextBuilder |

**Вывод**: роутер покрывает только user-chat путь. Любое единообразное решение должно либо (а) протащить роутер во все entry points, (б) дать альтернативный механизм задания complexity для не-user триггеров, либо (в) комбинировать оба — роутер для user messages, explicit hint для системных триггеров, propagation для agent-to-agent.

## 3. Proposed direction (high-level)

### 3.1 Domain primitives

```python
# src/domain/task_complexity.py
class TaskComplexity(str, Enum):
    SMALL_TALK       = "small_talk"
    INFO_SEARCH      = "info_search"
    SIMPLE_ANALYTICS = "simple_analytics"
    DEEP_REASONING   = "deep_reasoning"

# src/domain/complexity_settings.py
class ComplexitySettings(BaseModel):
    tier: PerformanceTier
    thinking_effort: Optional[str] = None
    provider_override: Optional[str] = None   # rare edge case
```

Семантические имена (не численные уровни) — LLM-роутеру проще классифицировать по смыслу задачи; код-level таблица прозрачно сопоставляет их с исполнительными параметрами.

### 3.2 Settings table

Дефолтная таблица в `src/infrastructure/agent_config.py`, user override через `UserBotConfig.complexity_settings_overrides`. Provider в дефолтах **отсутствует** — приходит из agent-level настроек (`user_config.get_provider_for_agent("smart")` или STRATEGIES default). Override провайдера per-complexity — рудимент для edge-кейсов.

Пример дефолта:
```
small_talk       → ECO
info_search      → BALANCED
simple_analytics → BALANCED + thinking=low
deep_reasoning   → PERFORMANCE + thinking=high
```

Точные значения — вопрос тюнинга после выкатки.

### 3.3 Resolution

`TaskExecutionResolver` (новый сервис) читает `message.context["task_complexity"]`, резолвит settings через таблицу + user override, строит `TaskExecutionOverride(llm, model_name, thinking_effort)` через расширенный `AgentContextBuilder.resolve_for_task(agent_type, user_config, settings)`.

Smart в начале `process()` вызывает resolver, применяет override ко всем turns внутри delegation loop (один провайдер на весь scope обработки сообщения).

### 3.4 Who sets `task_complexity` in `message.context`

Здесь и живут открытые вопросы §4. Кандидаты:

- **Router** — для user messages, как расширение текущего intent classification.
- **Worker handlers** — для системных триггеров, explicit hint (`task_complexity="deep_reasoning"` при создании AgentMessage).
- **AgentNote** — опциональное pinned поле, если reminder-автор (LLM или пользователь) хочет задать явно.
- **Inheritance** — для agent-to-agent: Smart делегирует суб-специалистам через coordinator, там complexity не переносится (суб-специалисты и так имеют свой fixed tier); но если Smart сам вызывает себя через coordinator (пока такого нет), complexity должен переноситься.

### 3.5 Dispatch simplification

Quick агент остаётся зарегистрированным в registry, но ConversationHandler его не диспатчит — все user messages идут в Smart с резолвленной complexity. Удаление Quick — отдельный PR (см. §6).

## 4. Open architectural questions

Эти вопросы блокируют финализацию дизайна. Не отвечены — имплементить нельзя.

### Q1. Reminders: route through router или pinned complexity?

**Опции**:
- **A**. Прогонять reminder alert text через роутер в `RemindersService.fire_due_reminders` перед `notify()`. Плюсы: один путь для всех сообщений. Минусы: +1 LLM call на каждый fire, +latency, +стоимость; роутер вызывается из сервиса, который раньше был чисто инфраструктурным.
- **B**. Pinned complexity на `AgentNote` (новое поле `complexity: Optional[TaskComplexity]`). NotesAgent LLM/Cabinet UI задают при создании reminder'а. Если пусто — дефолт (например, `simple_analytics`). Плюсы: 0 лишних LLM calls; reminder-автор знает желаемую глубину. Минусы: новый слой ответственности у NotesAgent (tool schema + prompt context), Cabinet UI reminder form получает ещё одно поле.
- **C**. Дефолт per entry source в коде. Reminders всегда получают `simple_analytics` (или `deep_reasoning`), без классификации. Плюсы: простейшее. Минусы: не умеет отличать «напомнить полить цветы» от «сделай утренний брифинг по инбоксу» — оба идут одним tier.
- **D**. Гибрид B+C: дефолт per entry source, но AgentNote может переопределить через pinned.

**Склоняюсь к D** — это и дёшево, и даёт control per-reminder.

### Q2. Daily email review и прочие worker tasks: explicit hint или router?

Аналогично Q1, но для worker-level триггеров:
- Daily email review уже передаёт `thinking_effort="medium"`. Логично оставить в том же стиле: worker-task код явно задаёт `task_complexity="deep_reasoning"` в `notify(...)` kwargs.
- Router здесь точно не нужен — worker-task разработчик знает характер payload'а лучше роутера.

**Предварительное решение**: **explicit hint в worker handlers**. Удалять `thinking_effort="medium"` не надо — он выигрывает у profile.thinking_effort (приоритет context).

### Q3. Agent → Agent delegation: propagation или fixed tier?

Сейчас суб-специалисты (EmailSearch, WebSearch, Maps, Compute) имеют свой tier, зашитый в AgentContextBuilder по `agent_type`. Smart делегирует им через coordinator — его complexity к ним не относится.

Но: **Smart после tool results переоценивает и может делегировать дальше**. Всё в рамках одной user message → одного Smart turn → одной complexity. Здесь ничего менять не надо.

Проблема возникнет если мы сделаем **Smart делегирует другому Smart-подобному оркестратору** (e.g. внутренний «reasoning» агент). Пока такого нет.

**Предварительное решение**: complexity — это параметр одного конкретного Smart-run'а, не пропагандируется вниз. Суб-специалисты используют свои фиксированные tiers.

**Но** — надо зафиксировать инвариант: если в будущем появится вложенный Smart, complexity должен либо пропагандироваться, либо переклассифицироваться на новом уровне. Решается когда понадобится.

### Q4. Safety net при неуверенности роутера

Сейчас CLAUDE.md говорит: «low confidence always falls back to Smart». С более тонкой градацией нужно решить:

- **Код-level fallback**: роутер вернул invalid / unknown / low-confidence → какой default? Не `small_talk` (снижает качество), наверное `simple_analytics`. Но: «simple_analytics» на абсолютно тривиальном сообщении — это переплата. Trade-off цена/качество в пользу качества — это консенсусно OK?
- **Prompt-level rule**: явное правило «при сомнении выбирай не ниже `simple_analytics`». Это и есть то же самое, только в промпте. Нужно оба уровня (промпт + код), потому что LLM может ошибиться.

**Open question**: Что конкретно считать «low confidence»? Роутер сейчас выдаёт confidence score в output, но порог (например, `< 0.7` → fallback) надо калибровать на реальных данных.

### Q5. Provider override per complexity — нужен ли в v1?

Пользователь сказал «оверрайд провайдера это очень редкий случай — дефолтно у агента один провайдер». Можно:

- **A**. Оставить поле `provider_override` в `ComplexitySettings`, но не показывать в v1 Cabinet UI (настройка только через API/JSON). Готов к расширению.
- **B**. Вырезать совсем до появления реального use case (YAGNI). Добавить позже.

Решение не критичное, но влияет на signature `ComplexitySettings` и объём тестов.

### Q6. Quick deprecation: синхронно или follow-up?

- **A**. В том же PR: удалить Quick, WebSearchLight, intent_remap, соответствующие тесты. Плюсы: чистая финальная картина. Минусы: большой PR, риск регрессии на трафике, который сейчас идёт в Quick.
- **B**. В этом PR только отключить диспатч в Quick (ConversationHandler всегда идёт в Smart). Quick живёт мёртвым кодом. Удаление — follow-up. Плюсы: легче откатить. Минусы: переходный период с dead code.

**Склоняюсь к B** — меньше риск-блока на один PR.

### Q7. `message.context` как канал — достаточно ли?

Сейчас `message.context` — это dict, который:
- инициализируется в handler / adapter,
- пропагандируется через DelegationEngine context passthrough,
- читается в агентах.

Проблема: `task_complexity` — это **метаданные about execution**, а не контент сообщения. Смешивать их с `origin_channel_id`, `session_id` и прочим — семантически грязновато.

Альтернатива: добавить отдельное поле `AgentMessage.execution_hints: Optional[ExecutionHints]` как value object.

Плюсы отдельного поля: типизированный канал, явный контракт, меньше «магических строк» в context dict.
Минусы: рефакторинг `AgentMessage` и всех call-sites; сейчас `thinking_effort` и прочие уже живут в context dict — разделение создаст несогласованность.

**Open question**: делать ли рефакторинг `AgentMessage.execution_hints` в рамках этого RFC или оставить всё в context dict как сейчас? Чисто архитектурный выбор.

### Q8. Router ответственность: остаётся intent classifier или расширяется до dispatch controller?

Сейчас роутер:
- Классифицирует complexity 1–10 (crude)
- Извлекает semantic lens + search intent
- Триггерит memory/web enrichment **до** роутинга

Если мы просим роутер ещё и возвращать `task_complexity`, его output schema расширяется. Это нормально. Но если потом reminders/email_review тоже начнут идти через роутер (Q1 opt A), роутер превратится из «понимает intent пользователя» в «универсальный диспатчер». Это **смена ответственности**, которую стоит явно зафиксировать или отвергнуть.

**Склоняюсь**: роутер остаётся intent classifier для user messages. Системные триггеры задают complexity explicitly. Это сохраняет единство ответственности и не размазывает роутер по разным вызовам.

### Q9. Сколько complexity уровней в v1?

Пользователь привёл 4 в пример. Возможные альтернативы:
- **3** уровня (`light`, `standard`, `deep`) — меньше decision fatigue у LLM роутера, но крупнее гранулярность.
- **4** уровня (приведённые в §3.1) — баланс.
- **5** уровней (добавить `research` выше `deep_reasoning`) — нужно только если deep research triage будет отдельно классифицироваться.

Решаем при финализации RFC. Архитектурно добавить/убрать уровень легко (enum entry + строка таблицы).

### Q10. Cabinet UI — что юзер видит и меняет?

- **Минимум**: таблица 4 строк, для каждой complexity — tier dropdown + thinking dropdown. Provider override скрыт (см. Q5).
- **Плюс**: дефолтные значения показаны серым, `is_overridden` флаг, «reset to default» per строка.
- **Плюс**: подсказки-примеры («small_talk: приветствия, yes/no…»), чтобы юзер понимал, что он крутит.

Не блокирующе, но надо решить до имплементации UI.

### Q11. Логирование и debuggability

Когда Smart исполнил с override — в логах и в `get_debug` / billing мы должны видеть:
- Какой complexity пришёл
- Как он резолвился (default vs user override)
- Какой финальный `(provider, model, thinking)` применился
- Кто выставил complexity (router / explicit / fallback)

Куда это писать? `AgentResponse.metadata`? отдельный event? `debug_logger`? Решается на этапе имплементации, но стоит зафиксировать в RFC что «трасса резолва — обязательный артефакт».

## 5. Non-goals / out of scope v1

- Account-level overrides (нет `AccountConfig` сейчас)
- Firestore-backed default таблица (пока в коде достаточно)
- Интеграция complexity с consolidation / deep research jobs (у них свой tier через job port)
- Динамический тюнинг complexity таблицы на основе feedback-loop (ML-ops)
- Multi-entry-point roll-out в один PR — должен быть поэтапный

## 6. Migration path (набросок)

1. **Phase 1**: инфраструктура — domain types, default table, resolver, `resolve_for_task`, wiring. Роутер **не меняется**, никто `task_complexity` не выставляет. Smart ведёт себя как раньше (override всегда None, старый path). Unit тесты. Ничего не ломается.
2. **Phase 2**: роутер начинает выдавать `task_complexity` для user messages; ConversationHandler пробрасывает в context. Quick остаётся живым, но не выбирается. Канарейка на dev, метрики.
3. **Phase 3**: reminders/worker tasks получают explicit hint. Пара итераций тюнинга дефолтов + критериев роутера.
4. **Phase 4**: удаление Quick + сопутствующих (follow-up PR после прод-бейка).

## 7. Appendix: критичные файлы для будущей имплементации

Оставлено как подсказка, не как обязательство:
- `src/domain/task_complexity.py`, `src/domain/complexity_settings.py` *(new)*
- `src/domain/user.py` — `complexity_settings_overrides`
- `src/infrastructure/agent_config.py` — `DEFAULT_COMPLEXITY_SETTINGS`, resolver
- `src/services/agent_context_builder.py` — `resolve_for_task`
- `src/services/task_execution_resolver.py` *(new)*
- `src/agents/router_agent.py` — output schema, Firestore prompt token
- `src/handlers/conversation_handler.py` — complexity в context, всегда Smart
- `src/agents/core/smart_response_agent.py` — override в delegation loop
- `src/agents/core/base_agent.py` — `_call_llm(llm_override)`
- `src/composition/service_container.py`, `src/composition/user_agent_factory.py`
- `src/handlers/worker_handler.py`, `src/services/user_notification_service.py` — explicit hint для системных триггеров (после Q2)
- `src/services/reminders_service.py` — поведение fire-path (после Q1)
- `src/domain/agent_note.py`, `src/adapters/firestore_agent_note_adapter.py` — pinned complexity (после Q1 opt D)
- `src/web/` — `/api/user/complexity-settings` GET/PUT (после Q10)
- Cabinet UI templates

## 8. Decision log

*Пусто. Заполнять по мере ответа на Q1–Q11.*
