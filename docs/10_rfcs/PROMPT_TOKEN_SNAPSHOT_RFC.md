# RFC: Prompt Token Snapshot — git mirror of the Firestore prompt layer

**Status:** Design approved — implementation pending
**Date:** 2026-05-30
**Author:** Dmytro Deleur (brainstormed with Claude)
**Related:** `docs/reviews/ARCHITECTURE_INSPECTION_FOLLOWUP.md` (Bucket I — token-as-source-of-truth subsystem, surfaced by R9A.6)

---

## 1. Problem

The bot's prompts are not hardcoded — they are assembled at runtime by the Prompt Builder (v4)
from **tokens**, **blueprints**, and **profiles** stored in Firestore. These documents *are* the
bot's behavior.

Today they live **only** in Firestore. The local working copies under `firestore_utils/uploads/`
and `firestore_utils/downloads/` are gitignored and have degenerated into a scratch drawer
(`...copy 2.groovy`, `.bak`, `_ORIGINAL`, `_V21`, mixed `.groovy`/`.json` for the same token).

Consequences:

- A prompt change cannot be reviewed in git — no diff, no PR, no history.
- Drift between Firestore (live, controlling the bot) and any local file is invisible.
- Concrete incident (R9A.6): a Firestore token carried a stray `class DocGeneratorAgent extends Agent {}`
  wrapper that the local `.json` did not — because someone uploaded the `.groovy` shape directly.
  It surfaced only by manually downloading and comparing.

For a system where prompts are the core behavior, having no version control over them is a
standing daily hazard.

## 2. Goal

A **git-tracked, diffable mirror** of the shared prompt layer, so prompt state is reviewable in
git and Firestore↔git drift is visible. Editing stays in Firestore; git is a read-only reflection.

## 3. Decisions (with rationale)

| # | Decision | Rationale | Rejected alternative |
|---|----------|-----------|----------------------|
| D1 | **Mirror, not source of truth.** Firestore stays authoritative; git is a read-only snapshot refreshed by a pull script. | Lowest risk — a pull can never corrupt live prompts. Matches current reality (Cabinet UI + manual edits write to Firestore). | Git-authoritative (GitOps push-to-Firestore) — bigger, riskier build; `upload.py` was made AI-forbidden precisely because direct uploads corrupted live tokens. Reconsider once the mirror exists. |
| D2 | **Shared layer scope.** Mirror `tokens_v3_system`, `tokens_v3_user` (both system-level), `blueprints_v3`, `profiles_v3`. Exclude `overrides_v3`. | `overrides_v3` is the only user-editable collection (account/user level → IDs + personal content → SECRETS RULE). The two token collections are both system-owned despite the `_user` suffix. | Mirror everything with redaction — leak risk if redaction incomplete; low value (personal overrides change rarely, are not "system behavior"). |
| D3 | **One file per Firestore document.** | Localized diffs — change one token → one file changes. Matches the mental model (token = unit) and `download.py` output. | Single config file (noisy whole-file diffs); per-collection file (token change diffs inside a large file). |
| D4 | **Format by document nature.** Tokens (text-heavy) → YAML frontmatter + readable body (`.groovy`). Blueprints/profiles (structured, no large text) → plain YAML. | Frontmatter+body gives clean prompt-text diffs with real newlines *and* captures metadata drift (catches R9A.6-class issues). Same shape as the project's own memory files. Round-trippable. | JSON full-doc (escaped `\n` makes multi-line prompt diffs unreadable); both `.groovy`+`.json` (doubles files, sync risk — YAGNI). |
| D5 | **Location: `prompts_snapshot/` at repo root, tracked.** | Top-level + explicit "snapshot/mirror" name signals "do not edit here, edit Firestore"; discoverable; zero entanglement with the gitignored `firestore_utils/` patterns. | `firestore_utils/snapshot/` — muddy (tracked data next to gitignored scratch), buries core artifacts in a utils dir. |
| D6 | **Config-driven environment binding.** The pull tool resolves the target Firestore + collection names from the same `env_config` the app uses — no hardcoding. | The env model is in flux (current env → prod after cleanup; a future *new* dev gets a separate Firestore). Collection renames and dev→prod relabel must flow through config without touching the tool or the layout. Filenames = document IDs, so renames don't churn files. | Hardcoding `development_*` names or a `dev`/`prod` path label — would break on rename and mislabel after dev→prod. |
| D7 | **Tooling is a script, no port.** Lives in `firestore_utils/` next to `download.py`/`upload.py`. | Dev-only tooling, single implementation, no runtime substitution need. Per CLAUDE.md "do not create ports for cleanliness". | — |

## 4. Repo layout

```
prompts_snapshot/                 # tracked, git-normal
  README.md                       # "read-only mirror of Firestore; source of truth = Firestore;
                                  #  refresh via firestore_utils/snapshot_pull.py; overrides excluded (PII)"
  tokens/
    system/<token_id>.groovy      # from domain_prompt_tokens_v3_system
    user/<token_id>.groovy        # from domain_prompt_tokens_v3_user  (system-level, separate namespace)
  blueprints/<blueprint_id>.yaml  # from domain_prompt_blueprints_v3
  profiles/<profile_id>.yaml      # from domain_prompt_profiles_v3
```

`tokens/system` and `tokens/user` are kept as separate subfolders so the snapshot is faithful to
the two source collections and round-trip stays unambiguous.

**Future two-environment case:** when a second Firestore env physically exists, the tool gains
`--env <label>`, writes to `prompts_snapshot/<label>/…`, and the current tree moves under its own
label with a single `git mv`. No multi-env machinery is built now (YAGNI).

## 5. Serializer

A small, round-trippable module (`firestore_utils/snapshot_serializer.py`). Mapping per document
shape (verified against the v4 repositories):

- **Token** (`token_id`, `category`, `class`, `content`, `metadata`):
  frontmatter = `token_id, category, class, metadata`; body = `content` (verbatim, real newlines).
- **Blueprint** (`blueprint_id`, `outer_class`, `class_order`): plain YAML of all three fields.
- **Profile** (`blueprint_id`, `tokens`): plain YAML of both fields.

Round-trip contract: `to_file(doc_dict) → text` and `from_file(text) → doc_dict` such that
`from_file(to_file(d)) == d` for every collection. This keeps the door open for a future
git→Firestore direction (D1's deferred alternative) without re-designing the on-disk format.

## 6. Commands

Single script `firestore_utils/snapshot_pull.py`:

- **`pull`** — read the mirrored collections from the config-resolved Firestore, serialize each doc
  to its file, **overwrite** `prompts_snapshot/`. Then `git diff` shows drift; `git commit` records it.
  Deletes snapshot files whose source document no longer exists (mirror = exact reflection).
- **`pull --check`** — serialize to a temp location, diff against the committed `prompts_snapshot/`,
  print the drift, **write nothing**, exit non-zero if drift exists. CI/pre-commit-ready. **Not wired
  to any hook now** — available when needed (YAGNI).

## 7. Safety (PII defense-in-depth)

PII exclusion is enforced by **mechanism, not by trusting collection names**:

- `overrides_v3` is excluded by scope (D2).
- The pull additionally **refuses to write any document** that carries a `user_id`/`account_id`
  field, or whose document ID matches a user-key pattern. If a mirrored collection is ever
  mis-scoped, nothing leaks — the guard trips and the doc is skipped with a loud warning.
- Implementation gate: before the first commit, verify by inspection that `tokens_v3_user` contains
  only named token definitions (no user-keyed documents), confirming D2's premise.

## 8. Testing

- **Unit:** serializer round-trip for each of the three forms (token / blueprint / profile) —
  `from_file(to_file(d)) == d`, including multi-line content, unicode (→ arrows in prompts), and
  empty `metadata`.
- **Unit:** PII guard — a doc with `user_id` / user-keyed ID is skipped; a clean doc is written.
- **Manual / integration:** `pull` against the live Firestore (read-only) — first run produces the
  initial snapshot; `pull --check` returns clean immediately after a `pull`.

## 9. Out of scope

- Git→Firestore push / GitOps (D1 deferred alternative).
- Mirroring `overrides_v3` (PII).
- Multi-environment subtrees and `--env` (deferred until a second Firestore exists).
- Wiring `--check` into CI or pre-commit.
- Cleaning or migrating the existing gitignored `firestore_utils/uploads/`+`downloads/` scratch dirs
  — left untouched.

## 10. Upload path (`snapshot_upload.py`) — brought forward 2026-05-30

Originally deferred, the upload counterpart was pulled forward because the first real pull
surfaced **indirect PII baked into system-token few-shot examples** (a real car: plate + town +
insurer, replicated across 7 consolidation/protocol tokens). The clean fix is to edit the snapshot
file and push it back to Firestore — which needs the upload path. The mirror earned its keep on
day one.

**Mechanism.** `snapshot_upload.py <file>...` "unwinds" each snapshot file via the existing
round-trip serializer (`token_from_file` / `doc_from_yaml`) into a Firestore doc dict, derives the
target collection + doc id from the file path, and upserts the token. It is the reverse of the pull.

**Decisions:**

| # | Decision | Rationale |
|---|----------|-----------|
| U1 | **Merge, not overwrite** — `set(doc, merge=True)`. | The snapshot strips top-level volatile keys (`created_at`/`updated_at`); a full `set()` would delete them from Firestore. Merge updates only the fields present in the file (e.g. the scrubbed `content`) and preserves the rest. |
| U2 | **Dry-run by default; field-level diff before any write.** `--dry-run` (default) prints the per-field diff (current Firestore doc vs file-parsed doc) and writes nothing. | Direct answer to the history where blind uploads corrupted live tokens (R9A.6). You see exactly what changes before it changes. |
| U3 | **`--apply` is human-only, hard-gated (Variant A).** `--apply` requires an interactive confirmation (`input()` echoing the exact doc id); in a non-interactive context it aborts on `EOFError` and writes nothing. AI may run `--dry-run`; AI must never run `--apply`. | Preserves the safety invariant behind `upload.py`'s AI-forbidden status while giving a clean, diff-gated tool. The TTY confirmation is a real mechanism — AI's non-interactive shell cannot satisfy it. |

**Scope (this iteration):** upload by file path(s); the script infers collection + doc id from the
path layout. `--changed` (push everything that differs from Firestore) and full git-authoritative
GitOps remain out of scope. `firestore_utils/upload.py` is left as-is (the legacy single-token tool).
