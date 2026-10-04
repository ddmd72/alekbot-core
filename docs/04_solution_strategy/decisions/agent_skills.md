# Decision: Agent Skills — skills as Smart-local tools (delivery A + B)

**Date:** 2026-10-04
**Status:** Shipped (delivery A, read path, AND delivery B, authoring + system skills). Full design: `docs/10_rfcs/AGENT_SKILLS_RFC.md`.

## Decision

Skills are named procedures (`SKILL.md`: trigger + body) served by Smart's own `use_skill` tool via
`DelegationEngine(local_tools=)` — never `delegate_to_specialist`, never `AgentCoordinator`. The
handler factory is `src/infrastructure/skill_tools.py` (not `agents/core/` — REQ-ARCH-24: no agent
imports a sibling `agents/` module). A loaded body persists as a raw `[Skill "<name>" v<n>]` block in
history until tiering compresses it to a neutral stub; the model reloads via `use_skill`. Delivery A
is read-only: seeded by `scripts/skills/seed_custom_skill.py`; owner authoring (`$skill save <code>`)
is delivery B, gated on `scripts/skills/skill_trigger_eval.py`.

## Rejected alternatives

- Skills as `delegate_to_specialist` intents — needs a `SkillsAgent` + caller identity + `mode`
  override; wrongly frames a procedure as a specialist.
- Background LLM author with six defence layers — detection after the fact is unreliable; a typed
  command (delivery B) authorizes instead.
- Lifecycle machinery (TTL/`release_skill`/pinning) — tiering plus a stub gets the same reload with
  no extra state.
- Addressable history (`expand_history(ids)`) — deferred; no logged case needs it yet.
- GCS version folders — no files in v1; Firestore's one-transaction 20 KB write suffices.
- Personal skills as system skills — repo is public; a personal procedure must never ship to everyone.

## Delivery B (authoring + system skills)

Owner authoring is a `draft_skill` local tool (interactive turns only) delivering a `skill_preview`
(verbatim `SKILL.md` file + `$skill save <code>` command); only the pasted code saves it. System
skills (`skill-creator`, `domain-competency-research`) ship in git and load via a plain
`load_system_skills` function rather than a second `SkillRepository` adapter — they are read-only,
so implementing the write port's drafts/delete on them would be a Liskov violation.

## Notes

- Index doc denormalizes `body` (plus `user_id, account_id, name, description, current, updated_at`)
  so every Smart request (incl. `notify()`/daily email review) reads up to 20×20 KB; revisit with a
  `select()` projection + an on-use body read if the catalog grows.
- `src/utils/capabilities.py` (`get_help`) is unchanged in A — nothing user-creatable yet; update it
  in delivery B.

## Revise if

- Delivery A's gate fails (trigger eval or real-use check) — B is reconsidered before any work on it.
- A non-Smart orchestrator needs skills — current wiring is Smart-only by construction.
