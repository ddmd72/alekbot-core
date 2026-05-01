# Decision: Per-Tier SLA Overrides on NotificationKind

**Status:** Adopted
**Date:** 2026-05-01
**Context:** Follow-up to NOTIFICATION_DELIVERY_REFACTOR_RFC after the
2026-05-01 morning fire revealed that the global ``REMINDER`` SLA of
600 s (10 min) is the right budget for a ``simple_analytics`` reminder
but is too tight for a ``deep_reasoning`` reminder that may run
multi-turn delegations with web research and a document hop.

## Decision

``NotificationSLA`` is extended with an optional
``tier_overrides: Mapping[PerformanceTier, int]``. ``REMINDER`` is the
only kind that uses overrides today:

| Tier        | Timeout | Use case                                           |
|-------------|---------|----------------------------------------------------|
| ECO         |   3 min | small_talk reminder — single LLM call              |
| BALANCED    |  10 min | info_search / simple_analytics — current default   |
| PERFORMANCE |  25 min | deep_reasoning — full multi-turn analytical work   |

Other kinds (``INTERACTIVE``, ``DAILY_DIGEST``, ``DOCUMENT_DELIVERY``,
``DEEP_RESEARCH``) keep no overrides — their work envelope is fixed by
purpose, not by model tier.

## Why per-tier on REMINDER specifically

Only ``REMINDER`` realistically varies in complexity at the level of an
individual fire:
- The user (well, the orchestrator writing reminders to itself) sets a
  per-note ``complexity`` that already drives model selection via
  ``TaskExecutionResolver``.
- Same complexity should drive the budget that bounds wall-clock work.

The other kinds are uniform by design:
- ``INTERACTIVE`` is bounded by user-on-the-other-end UX — adding a
  longer budget for ``PERFORMANCE`` would just make the human wait.
- ``DAILY_DIGEST`` is one analytical pass, sized for the worst case.
- ``DOCUMENT_DELIVERY`` is formatting-only.
- ``DEEP_RESEARCH`` is a kick-off — the long work runs in a Cloud Run
  Job, not under this budget.

## Resolution

```python
sla = NOTIFICATION_SLA[kind]
timeout = sla.tier_overrides.get(tier, sla.timeout_ms) if tier else sla.timeout_ms
```

The ``RemindersService`` worker (``_handle_execute_reminder``) reads
``note.complexity`` and resolves the tier through the same
``DEFAULT_COMPLEXITY_SETTINGS`` table that ``TaskExecutionResolver``
uses — single source of truth for the complexity → tier mapping.

## Trade-offs

- **+** Reminder budgets now match work envelope; no more 30.04-style
  near-miss when a deep_reasoning reminder uses 6 minutes of a 5-minute
  budget by chance.
- **+** Single-source-of-truth preserved: complexity → tier lives only
  in ``DEFAULT_COMPLEXITY_SETTINGS``.
- **+** Backwards compatible: callers that don't pass ``tier=`` get the
  kind default.
- **−** Two parameters in ``notify`` (kind + tier) instead of one. The
  alternative — passing a single ``(kind, tier)`` enum — would explode
  the surface area for kinds that don't vary.
- **−** PERFORMANCE reminders now occupy a Cloud Run worker for up to
  25 min. At current scale (single user) this is fine; if reminder
  fan-out grows substantially, either reduce per-tier maximums or move
  ``deep_reasoning`` reminders to Cloud Run Jobs (mirrors DeepResearch).

## Mechanical enforcement

- ``tests/unit/infrastructure/test_notification_sla.py`` —
  ``TestReminderTierOverrides`` (5), ``TestNonReminderKindsHaveNoOverrides``
  (parametrized over the four other kinds), ``TestResolveTimeoutMs`` (4).
- ``tests/unit/services/test_user_notification_service.py`` —
  ``TestNotifyTierResolution`` (4) verifies notify() actually consumes
  ``tier=`` and propagates to ``AgentMessage.timeout_ms``.
- ``tests/unit/handlers/test_worker_handler.py`` —
  ``test_tier_forwarded_per_complexity`` parametrized over all four
  ``TaskComplexity`` values; ``test_tier_is_none_when_complexity_absent``.
- Coverage gate (``make test-coverage``) keeps the 100% floor on
  ``user_notification_service.py`` and ``reminders_service.py``.
