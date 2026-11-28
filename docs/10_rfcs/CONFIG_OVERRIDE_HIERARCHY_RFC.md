# RFC: Configuration override hierarchy — SYSTEM → ACCOUNT → USER

**Status:** Accepted (2026-11-25); step 1 implemented (2026-11-28), steps 2–7 not built.
**History:** a point-in-time design record; how it works now: root [CLAUDE.md](../../CLAUDE.md) § Where things live.
Direction and the owner decisions in §5 are the owner's and final. Slices, cards and the test plan: the private
delivery plan.

## 1. Problem

Configuration is resolved bespoke, knob by knob:

- `ConfigurationService.get_effective_config` / `_merge_configs` has no production caller and has the wrong shape:
  "override if different from the default" cannot express "explicitly set to the default", and it returns the whole
  account object.
- The account level is applied through copy-paste per-field getters (search, history and cache limits, the video
  cap). Tiers, providers, thinking and model pins have no account level at all; the video cap declined one; the
  biographical and principles cache limits are read two different ways.
- Storage is dense: a user is saved as a full `model_dump()`, and an account gets `account_defaults=UserBotConfig()`
  at creation, so "unset" is lost for every non-empty default (`agent_tiers` freezes the defaults of the save date).
- The agent factory's cache TTL slides on every hit: an active user never sees a Firestore edit until a restart.
- Dead fields and APIs accumulate (`tools_enabled`, `is_paranoid_mode`, `memory_search_limit`, `temperature`,
  `PromptPreferences`, `ComplexitySettings.intent_remap`, the merge API, the `bio_keywords` getters).

The first feature that needs a real hierarchy is per-user and per-account **intent availability**: turning an agent's
capability off for one user or for a whole account.

Prompts already resolve SYSTEM → ACCOUNT → USER correctly, and they do so because of three properties, not because of
the number of levels: every override level is **sparse** (absent = inherit), each setting has **one identity key**,
and a higher level can **lock** a key (`non_overridable`). This RFC copies those three properties for all
configuration.

## 2. The rule (one, for everything)

The effective value of a key is taken from the most specific level where it is set: USER, else ACCOUNT, else SYSTEM.

- Levels store only what they change; removing a key returns it to inherit.
- A lock at a higher level stops lower overrides (added when an account writer exists, §6).
- Some fields declare which levels accept them:
  - **USER only** — personal facts and consents: timezone, languages, voice, Gmail schedules,
    `drive_copy_deep_research`.
  - **ACCOUNT only** — account resources: the biographical and principles cache limits.
  - **SYSTEM only** — constraints: `allowed_providers`, capability gates, environment kill switches.

**Code shape.** One pure domain primitive walks the levels per key (first set wins, a lock stops the walk), and one
service method, `ConfigurationService.effective_for(user_id)`, loads the user and the account through their ports and
returns a frozen `EffectiveConfig`. Every consumer reads through it; a new setting is one field of the levels and one
of the result, not a new getter.

## 3. Step 1 as built: the resolver and intent availability

### 3.1 Levels and the resolver
- `src/domain/config_resolution.py`: `resolve(system, account, user) -> EffectiveConfig`. Pure (no I/O, no logging).
  Step 1 carries one setting; later steps add fields to `SystemConfig` and `EffectiveConfig` without changing the
  signature.
- SYSTEM level: `SystemConfig.intent_access` — every **gated** intent and its default availability, built in
  composition from the agent manifest. Its key set *is* the set of gated intents: an entry at a lower level for a key
  the SYSTEM level does not declare is not a setting and is ignored.
- ACCOUNT and USER levels: `UserBotConfig.intent_access: Dict[str, bool]` on `BillingAccount.account_defaults` and on
  `UserProfile.config`. Stored sparse: only differences from the inherited value.
- `EffectiveConfig.is_intent_available(intent)`: the resolved value; an ungated intent is always available.

### 3.2 The SYSTEM default lives on the agent
- `AgentDescriptor.available_by_default: bool = True` — the default for all of the agent's intents.
- `AgentDescriptor.access_gated: bool = True` — `False` for plumbing agents that another agent calls as a step of its
  own work, whose denial would break the caller rather than remove a feature: the DOCX generator (called by the
  document planner) and the Alek gateway (called by Lelik). Their intents are never gated. The existing `internal`
  flag means "not offered to models" and is a different thing: bound-channel companions are internal and gated.

### 3.3 The service method
`ConfigurationService.effective_for(user_id)` reads the user, then the user's account, fresh on every call (no cache:
a Firestore edit applies to the next delegation).
- No account doc, no `account_id`, or no `account_defaults` → no account level.
- A read error → SYSTEM defaults and a logged warning (owner decision: not fail-closed).

### 3.4 The gate
The enforcement point is `AgentCoordinator.handle_delegation`: before dispatch it asks a fresh `effective_for` (injected
from composition) whether the intent is available, and refuses with a typed marker (`AgentResponse.error_code =
INTENT_UNAVAILABLE`, status failed). Every delegation path goes through it:

| Path | How it reaches the gate |
|---|---|
| Orchestrator tool calls (Smart, Quick, Tutor, DomainResearcher) | the delegation engine's dispatch |
| Lelik (voice) | `LelikAgent.delegate_outcome` → the delegation engine |
| Notes | its direct `handle_delegation` call |
| Bound channels | the conversation handler's direct call for a bound turn |
| ASYNC intents | at enqueue (the queued task later runs without a second check) |

Rendering of a refusal:
- The delegation engine returns a tool result `Tool error: <intent> is turned off for this user; calling it again gets
  the same answer.` — without the "correct your input and try again" line it adds to other specialist failures, since
  retrying cannot help. It names no level, so it holds whether the user, the account or a SYSTEM default turned it off.
- Notes shows the failure's error text to its model (`Tool error: <error>`) instead of "No result".
- Fan-out: the engine checks the primary intent before dispatching secondaries; a denied primary dispatches nothing.
  Denied secondaries are dropped before dispatch, so the merged answer never claims a specialist answered that was not
  asked; with none left, the primary runs as a plain single dispatch.
- Lelik checks availability itself before dispatching and returns a voice-safe note with no intent id in it (`Tool
  error: this request is turned off for this caller; asking again gets the same answer.`), the same way it refuses
  intents a voice call does not permit.
- Bound channels: a bound turn to a denied agent tells the user the agent is not available on their profile; binding
  a channel to a denied agent (`$agent`) is refused.

### 3.5 The declaration filter
Each orchestrator (Smart, Quick, Notes, Tutor, DomainResearcher, Lelik) builds its delegate tool from the intents the
coordinator returns for the current user (`AgentCoordinator.available_intents_for`), so a denied intent is not offered.
The filter is a courtesy; the gate stays the enforcement: the shared agent-selection prompt token still names every
agent. Because that token keeps describing the turned-off intents, the declaration says so when, and only when, the
profile filter removed one (a sentence telling the model not to call the intents missing from the list and to tell the
user they are turned off); other removals (Lelik's native search, Smart's external-tasks offer) do not add it. When the
filter removes every intent, Smart and Quick still declare the tool, with "none: every specialist is turned off for this
user" as its list; Tutor, DomainResearcher and Notes omit an empty tool as before.

## 4. The seven steps (one pull request each)

1. **Primitive + resolver + intent availability** (§3).
2. **Limits onto the resolver:** search, history and cache limits, the video cap (gains an account level), the fact
   review interval, the consolidation threshold and batch (deciding the "0 means unset" reading). The getter tests are
   retired as moved. Biographical and principles limits become ACCOUNT-only.
3. **Tiers, providers, thinking, model pins** onto the resolver with an account level; `agent_tiers` defaults to `{}`;
   a data script strips stored values equal to the current defaults (dry run and a diff for the owner first: users with
   stale pinned tiers move to the current defaults).
4. **Freshness of the agent snapshot:** an absolute maximum age instead of the sliding TTL; Cabinet writes evict the
   user.
5. **System skills:** the availability default in the skill's own description (SYSTEM), `skill_access` at ACCOUNT and
   USER, checked by the skill service when listing and when opening (skills are Smart's local tool, not a coordinator
   intent).
6. **Prompts onto the same primitive**, keeping their own storage. First a check that the prompt semantics (an override
   replaces only an existing token, the class + category key, order, the 24 h cache, the epoch guard) fit the primitive;
   anything that does not fit goes to the owner, not bent silently.
7. **Delete dead configuration code:** the merge API and its tests, `tools_enabled`, `is_paranoid_mode`,
   `memory_search_limit`, `temperature`, `PromptPreferences`, `ComplexitySettings.intent_remap`, the `bio_keywords`
   getters.

## 5. Owner decisions (2026-11-25, binding)
- A read error resolves with SYSTEM defaults and logs a warning; it does not fail closed.
- A user whose account has no account doc has no account level.
- Bound channels obey the gate, and binding a channel to a denied agent is refused.

## 6. Limits (stated, not built)
- **Access lock.** An account deny can be re-enabled by a user allow today. A lock comes with the account writer, as a
  separate account-level `locked_*` set (like `non_overridable`); `intent_access` needs no migration for it.
- **An intent is not a feature.** Denying the email intent does not stop the daily email review, the drive MCP
  connector or `$` commands. A "feature off" need gets its own gate at that entry point.
- **ASYNC tasks queued before a deny still run.**
- **A step another feature takes internally is not gated by the step's own intent.** Deep research delivers its report
  by enqueueing the HTML page generator directly, so denying `create_html_page` does not stop the report page of an
  allowed deep research.
- **No editor.** No Cabinet editor and no account admin UI until a need exists; edits are made by hand in Firestore.
