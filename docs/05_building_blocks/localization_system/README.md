# Localization System (Building Block)

## 📖 HowTo: Using This Document

### Purpose
Describe how semantic status messages map to localized UI strings across adapters.

### When to Read
- **For AI Agents:** Before introducing new status types or UI phrases.
- **For Developers:** When adding languages or updating localized phrases.

### When to Update
This document MUST be updated when:
- [ ] `StatusType` enum changes.
- [ ] New language packs are added.
- [ ] Localization selection logic changes.

### Cross-References
- **Observability Strategy:** [../observability_strategy/README.md](../observability_strategy/README.md)
- **Slack Dual Mode:** [../slack_dual_mode/README.md](../slack_dual_mode/README.md)

---

## 1. Overview
Localization is a platform-agnostic system for UI messaging. Adapters request
phrases by semantic `StatusType`; language packs provide the actual strings.

## 2. Architecture Layers
1. **Domain:** `StatusType` in `src/domain/ui_messages.py`.
2. **Infrastructure:** language packs in `src/locales/`.
3. **Adapters:** request localized phrases via `ResponseChannel`.

## 3. Status Types
Defined in `src/domain/ui_messages.py`:
- `THINKING`
- `SEARCHING_MEMORY`
- `SEARCHING_WEB`
- `PROCESSING_FILE`
- `ERROR`

## 4. Language Packs
Current locale files:
- `src/locales/uk.py` (primary)
- `src/locales/en.py` (stub)

Each pack provides a list of phrases per status and a `get_message()` helper.
Random phrase rotation reduces repetition.

## 5. Adapter Usage
Adapters call:
```python
phrase = await response_channel.get_status_phrase(StatusType.THINKING)
```
They do not embed user-facing strings directly.

## 6. Adding a New Language
1. Create `src/locales/{lang_code}.py`.
2. Implement message lists per status.
3. Add `get_message(status_type)` helper.
4. Wire language choice via user profile (planned).

## 7. Code References
- `src/domain/ui_messages.py`
- `src/locales/uk.py`
- `src/locales/en.py`
- `src/domain/messaging.py` (ResponseChannel API)

## 8. Status
**Production Ready** (Ukrainian primary; English stub).