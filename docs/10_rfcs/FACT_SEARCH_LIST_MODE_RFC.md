# RFC: Fact search list mode — `scope=all` as a boolean set per phrase

**Status:** Design agreed with the owner 2026-11-11. Implemented in `src/domain/fact_search_lists.py` and
`src/services/fact_search_service.py` (§8), except the z-score floor of §3.3, which is not built.
**Owner:** Dmytro Deleur.
**Amends:** `FACT_PROFILE_RFC.md` §5 (search). The rows and paragraphs it changes are listed in §7.

## 1. Context and problem

A fact query (`src/domain/fact_query.py`) carries `scope`: `top` (the first 30) or `all` (collect every match). Today the
two scopes share one algorithm (`FACT_PROFILE_RFC.md` §5.3). Every list generates candidates: the semantic, labels and
message kNN lists, the key lists (domain, time, kinds, places, people) and the home list. The linear scorer orders the
union, and the near-duplicate pass keeps the first 30. `scope=all` only lifts that cut:
`keep_distinct(fused, is_duplicate, None if query.scope == SCOPE_ALL else CUT)` (`src/domain/fact_search_lists.py:340`).

That gives the wrong answer to "list everything" requests:

- **The set is defined by nearness, not by the keys.** The kNN lists bring their 60 nearest whatever the keys say, and
  the home list (§5 of `FACT_PROFILE_RFC.md`: facts at the home town when the query names no place) brings home-town
  facts. "All my gym habits" then returns gym habits plus whatever sits near the words.
- **The capped key lists lose members.** The domain list is cut at `DOMAIN_CAP` and the home list at `HOME_CAP`
  (`fact_search_lists.py:24`, `:278`, `:286`). Their in-list order is by time role, so states sort newest first
  (`order()`, `:157`). On a large domain the cap drops the oldest facts, however relevant they are.
- **The scope applies to the whole request.** `src/services/fact_search_service.py:134` lifts the cut for every phrase
  when any phrase asks for `all`. Open PR #359 makes this per phrase, and this RFC is written against the per-phrase
  behaviour.

## 2. Two modes per phrase

Each phrase of a request carries its own `scope`, and the two scopes are two algorithms.

- **`top`** stays as it is: the wide candidate net (every list of §5.3), the fitted linear scorer, multi-phrase fusion
  and the cut to 30. One thing changes, in §5.
- **`all`** adds an algorithm to `top`. The phrase's structural keys define a set, and the phrase returns that set
  whole, on top of what it would return as a `top` phrase.

A request is a union of strategies. Every phrase, an `all` phrase with set keys included, runs `top` exactly as a `top`
phrase does (same lists, scorer, cut and fusion). Each `all` phrase with set keys also supplies the complete set its keys
define. The request's result is the fused `top` result, then the members of every `all` set it lacks, in union and
without duplicates: each set in its own order (§3.2), set after set in phrase order. A fact found by several phrases
appears once and names every phrase that found it, in the one result format of PR #359.

Why the set never replaces `top`: the slots model often infers `kinds` and `people` keys that the needed facts lack, so
a strict set can come back empty, and a phrase that no longer ran the scored search lost the facts it used to find. With
`top` kept and the set only adding, recall held at the level of the scored search while results became much smaller.

## 3. The `all` set

### 3.1 Membership: a boolean filter over the structural keys

The structural keys are the four slots that the profile stores as exact fields:

| Key | Matches a fact when |
|---|---|
| domains | the fact's `domain_keys` hold the domain |
| kinds | the fact's stored `kinds` hold the kind |
| people | a mention matches by name or relation, as `person_match()` does today |
| named places | the fact's `place_keys` meet the atlas keys of the named place (and its `lp:` key) |

- **Values within one key are joined by OR.** Two domains give the facts that carry either one. The "both first, then
  either" grouping of the `top` domain list does not apply here.
- **Different keys are joined by AND.** A fact is in the set only when every key the phrase names matches. The filter
  is strict.
- **A domain is a key only when it stands alone.** The phrase's domains take part in membership only when it has none
  of kinds, people, a resolved named place or a time window; otherwise they are ignored for the set (`top` still uses
  them for its domain list). The slots model infers domains the question never named, and the strict AND would then
  drop every fact stored under another domain (a bicycle stored under belongings for "my transport"). A domain-only
  phrase ("everything about my health") still filters by domain. The result's `keys` list only the keys that formed
  the set.
- **A time window is a filter too.** The `period`, `future` and `past` intents add one more AND condition: a point of
  the fact inside the window (a recurring occasion by month and day, as in §5.3).
- **`latest` is not a filter.** It only sets the output order (§3.2).
- **No nearness list takes part in the set.** The semantic, labels and message kNN lists do not define it (they feed the
  phrase's `top` run, §2). The home default list is not used either: a phrase that names no place has no place condition, and the home town plays no part.
- **No count cut.** `DOMAIN_CAP`, `HOME_CAP` and `CUT` do not apply. The near-duplicate pass still runs over the set
  with no limit, as `scope=all` does today. It removes copies, not members.

### 3.2 Order inside the set

The set is ordered by closeness to the phrase: the cosine of the phrase's subject to the fact text, the similarity the
scorer already computes for every snapshot row. A phrase without a subject uses the message cosine, which is also
computed for every row. Under the `latest` intent the order is newest point first, with closeness breaking ties. The
linear scorer is not run: it ranks candidates against each other, and the set has no cut for a rank to decide.

### 3.3 Closeness never decides membership by default

The probes showed that the embedding space is compressed. At the margin, relevant and irrelevant facts sit at the same
cosine to a phrase, so no fixed cosine threshold separates them. Closeness therefore only orders the set.

**Option, off by default: a z-score floor.** The cosine of a fact is standardised against the phrase's cosines to the
whole base (mean and spread over every row of the snapshot). A fact below the floor leaves the set. It is turned on only
if the acceptance requests (§8) show a broad key pulling junk into a set. The floor value is then set from those
requests.

### 3.4 A phrase with no structural key

An `all` phrase whose slots carry no domain, kind, person, named place or time window (a subject only) has no set to
define. It runs `top` alone, as every `all` phrase does (§2): the full net, the scorer and the cut.

A time window on its own does define a set: "everything from last March" asks for exactly the facts dated in that
window, the way "everything about Clara" asks for the facts naming her. Such a slice can be large, and that is its
answer; the result is ordered by closeness to the phrase like any other set.

### 3.5 The intimate gate

Today `scope=all` with people named opens the intimate gate on every list (`fact_search_lists.py:241`). Otherwise an
intimate fact survives only in the labels list and its own domain list (§5.3). The `all` set applies the same gate. An
intimate fact stays in the set when the phrase names people, or when it carries a domain that formed the set (§3.1: a
domain ignored because the phrase has other keys never opens the gate, since the slots model usually inferred it).
Otherwise the gate drops it.

This stays open. The owner chose not to change the gate now, because there is a sole user.

## 4. Examples

All examples come from the invented universe of `tests/CLAUDE.md`. Springfield is the user's home town.

- **"everything about Clara".** One key: people, `Clara` (the partner). The set is every fact that names Clara or carries
  the partner relation, ordered by closeness to the phrase. Because people are named, the intimate gate is open.
- **"everything about the Skoda Fabia".** The slots carry the domain `transport` and the kind `ownership`; the domain
  is ignored (§3.1), so one key forms the set: the facts that carry the ownership kind, whatever their domain, ordered by
  closeness to "Skoda Fabia". A transport fact without the ownership kind is outside the set. Which keys a phrase carries is the slots' business (§6).
- **"all my gym habits"** (the Iron Temple gym). A topic with a domain. Two domain values, `sport` and `lifestyle`,
  joined by OR, give the facts that carry either one. The phrase names no place, so no Springfield facts join through the
  home list as they would under `top`. Facts about the gym itself rise to the top only by closeness.

## 5. Change to `top`: capped lists ordered by closeness

The in-list order of a key list only feeds its cap and the log, not the ranking. It matters wherever a cap cuts. Under
`top`, the two capped lists are ordered by closeness to the phrase (the subject cosine, else the message cosine), not by
recency:

- **The domain list (`DOMAIN_CAP`, `fact_search_lists.py:272–278`).** With two domains, the facts that carry both still
  come first, and each group is ordered by closeness.
- **The home list (`HOME_CAP`, `:283–286`).**

The uncapped key lists keep their present order, which ranks nothing.

**Finding, not a change.** The probes showed that the whole-base semantic list adds no unique fact once the key lists
exist: every fact it brings, another list brings too. This RFC does not drop it. Follow-up: measure dropping the semantic
list from `top` on the acceptance set, then decide.

## 6. Prerequisites and open questions

**Prerequisite: slot fixes, in a separate PR before this one.** They change the token `OUTPUT_FORMAT_FACT_QUERY_SLOTS`:

- `latest` is for one changing thing, never for habits or preferences.
- "At home" as the setting of a habit is not the home town. The town is set as a place only when the place is what
  separates the wanted facts.

**Open: who decides `all`.** Today the slots model sets `scope`, and it is unreliable at it. One direction: the caller
(Smart, the claude.ai connector) passes the list intent itself. There is no design yet.

**Open: a key for who a fact is about** (the user, another person, or both). The profile has no such field today, so a
set cannot be limited to the user's own facts. There is no design yet.

**Open: the intimate gate under `all`** (§3.5).

## 7. Exact amendments to `FACT_PROFILE_RFC.md` §5

| Where in §5 | Today | After this RFC |
|---|---|---|
| §5.1 `scope` | `top`/`all` per query | per phrase; `all` selects the set of §3, falling back to `top` per §3.4 |
| §5.3 lists table, row **domain** | "in-list order, capped at 100" | `top`: ordered by closeness to the phrase (both-domain facts first), capped at 100. `all`: not a list; the domain is a filter key (OR within, no cap) |
| §5.3 lists table, row **home** | "in-list order, capped at 60" | `top`: ordered by closeness to the phrase, capped at 60. `all`: not built |
| §5.3 lists table, rows **semantic**, **labels**, **message** | run whenever their text exists | unchanged: built for every phrase, an `all` phrase with a structural key included (§2) |
| §5.3 lists table, rows **time**, **places**, **people**, **kinds** | candidate lists | unchanged for `top`; for the set of an `all` phrase they are also AND conditions (`latest` orders only) |
| §5.3 "Intimate gate" | `scope=all` with people opens it | unchanged for `top`; for the set of an `all` phrase as §3.5 |
| §5.3 "Scoring", last sentence | "`scope=all` keeps every candidate after dedup" | the set of an `all` phrase is not scored: it is ordered by closeness (§3.2), and dedup runs with no limit |
| §5.3 "Several phrases" | "`scope=all` on any phrase keeps every fused fact" | every phrase fuses and cuts to 30 among themselves; the result is that, then the members of the `all` sets it lacks (§2) |

Every other part of §5 is unchanged, including the scorer, its weights, the windows and the recurring-occasion rule.

## 8. Where it lands and how it is accepted

- **`src/domain/fact_search_lists.py`:** an `all` path beside `run_search`. It builds the set by the filter of §3.1 over
  the snapshot, applies the gate of §3.5, orders the set as in §3.2 and runs `keep_distinct` with no limit. The
  closeness order for the two capped lists of §5 also lands here.
- **`src/services/fact_search_service.py`:** every phrase makes its kNN calls and runs `top`; an `all` phrase with set
  keys also runs its set. All phrases are fused as today, and the result is the union of §2.
- **The `[FactSearch]` log line** marks each phrase with what it ran: `top`; `all[keys]+top` for an `all` phrase with
  set keys (the keys that defined its set, `+`-joined, then `+top` because it ran both); `all>top` for an `all` phrase
  without one. A key the phrase named that reached nothing is added inside the brackets: `all[domains;unresolved:place]+top`,
  `all>top[unresolved:place]`. `lists=` describes the first phrase and ends with `set:N` when that phrase has a set.
- **Acceptance** uses the local control requests (gitignored) and adds list requests of the three shapes in §4. The
  z-score floor of §3.3 is decided on them.
