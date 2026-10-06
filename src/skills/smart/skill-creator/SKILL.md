---
name: skill-creator
description: "Use when the owner asks to keep, save or turn into a skill a procedure, method or routine — or agrees after you offered to save one — and when the owner wants an existing skill changed."
---
# Creating and editing skills

A skill is a named procedure you load on demand with `use_skill`. The owner keeps procedures as skills so you can repeat them next time without being taught again. Only the owner can save one: you prepare a draft, the system shows it to the owner as a file with a save command, and the owner pastes the command. You never save anything yourself.

## Capture before you ask

The procedure is usually already in this conversation: the steps the owner explained, the sources and tools you used, the corrections the owner made, the output format that worked. Extract it from there first. Ask only about real gaps, in one short message — not a questionnaire.

For an existing skill, load it with `use_skill` first and change only what the owner asked for. Built-in skills cannot be changed or replaced under the same name; if the owner wants a variant, draft it under a new name.

## Write it the way skills work best

- **name**: short kebab-case, e.g. `weekly-vendor-report`.
- **description** — the trigger, one line, starting with "Use when …". It is the only part you see before loading, so make it specific and a little pushy: name the situations and phrasings that should load it, including ones that do not use the obvious keyword. Models tend to under-use skills.
- **body** — a set of available actions and why each exists, not a rigid step list: the next run may need a different order. Explain the reasons behind rules instead of shouting MUST/NEVER; give the exact values that matter (URLs, field names, formulas, formats); keep it under ~500 lines. Write it in English; the answers it produces follow the owner's language.
- Keep personal data in it only when the procedure needs it — the skill is the owner's alone, but it is still a stored instruction.

## Keep the body lean: files

The body holds when to use the skill and what to do. Material needed only sometimes — reference tables, long lists, examples, output templates — goes into a file at `references/<topic>.md` (or `.csv`, `.json`, `.yaml`), with a line in the body saying when to open it, e.g. "When you need the fee table, open `skill:<name>/references/fees.csv`". The body is loaded every time; a file costs nothing until it is opened.

- `files=[{path, content}]` — text you write; at most 5 per draft.
- `files=[{path, from_file: "<filename from the file label>"}]` — keep a file the owner sent, copied as is. Never retype it: a copy cannot introduce mistakes. `from_file` also takes `skill:<name>/<path>` from one of the owner's own skills, never a built-in one.
- A revision lists only changes: unlisted files carry over unchanged; `{path, remove: true}` drops one.
- Text only: `.md .txt .csv .tsv .json .yaml .yml`; 256 KB each; 20 per skill.

## Hand it over

Call `draft_skill(name, description, body, files=[...])` (`files` only when there are any). The owner then receives the full text as a file, every file you wrote as its own file, a summary of the file changes, and a separate message with a save command — the command is shown only to the owner, not to you. Tell the owner, in one or two sentences, what the skill does and that pasting the command saves it. Do not claim it is saved: a saved skill appears in this conversation as a system note.

If `draft_skill` reports a problem (name format, size, a flagged phrase), fix it and draft again.

## After saving

Suggest a real trial **in a new thread**: here you still see the conversation the skill came from, so a trial in the same thread proves little. Warn when trying the procedure has real side effects (tasks, reminders, messages sent). Revising is the same loop: a new draft with the same name, then the owner saves it as the next version.
