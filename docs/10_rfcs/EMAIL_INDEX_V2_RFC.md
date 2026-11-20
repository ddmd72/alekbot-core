# RFC: Email index v2 — one card per mail, keyed search on the drive's engine, a re-card of stored ids

**Status:** Slices 0–10 and 9b built (per-slice table in §9).
**Date:** 2026-11-16 (design 2026-11-13; as-built deltas folded in).
**Builds on, does not repeat:** fresh headers + exact keys (`decisions/email_search_fresh_headers.md`): code, not Smart,
lists mail newer than the index and literal Gmail matches of up to 3 identifiers. v2 changes what the **index** holds
and how it is searched; the two live lists stay as they are.
**Supersedes:** `GMAIL_EMAIL_INDEXING_RFC.md` §2.4 (search), §3.2 (stored entity) and §4 (the classifier's fact; slice
9b). Building-block map: `docs/05_building_blocks/gmail_email_indexing/README.md`.
Every example is invented.

---
## 1. Problem and goals

**What the owner asks about mail** (the typical shapes of an `email_search` request): a specific mail by a number or a
counterparty ("the Albergo Immaginario booking", "invoice FA-0137"); a value inside a mail (an amount, dates, a
reference); what came in a period ("what did Example School send in March"); everything of a kind ("all insurance mail
from Example Mutual"); what is coming ("when is the flight", "is anything due this month"); something about a family
member (a parents' meeting about Jamie); a document carried by the mail (a lab report PDF, contract terms); and the
newest mail (served by fresh headers already).

**Why it fails today — mechanisms:**
1. **The index entry is one sentence written from a glimpse.** The classifier sees subject, sender, date and a
   300-character snippet per mail in one batched call (`src/agents/email_classification_agent.py:367-375`), may fetch a
   body cut at 3 000 characters (`:337`), and writes one English past-tense "fact"
   (`prompts_snapshot/tokens/system/EMAIL_CLASSIFIER_TAXONOMY.groovy:61`). That sentence is the whole searchable content
   (`src/services/email_indexing_service.py:798`): the body is never stored or embedded, the subject is not in it, a
   printed id or amount survives only if the model copied it, future bookings are bent into the past, and a mail about a
   child reads as a mail about "the user".
2. **Four vectors vote on one signal** — fact, tags, subject+sender+month+fact, file names (`email_indexing_service.py:747-781`).
   Seven kNN streams fused by RRF (`src/services/email_search_service.py:119-144`) cannot abstain: the cosine cut 0.4
   (`src/adapters/firestore_indexed_email_repo.py:36`) filters almost nothing in a compressed space; each of the two calls
   keeps its top 20 and the merge has no cap (`email_search_service.py:143`), so up to 40 lines come back for a nonsense
   query, and near-identical template mail (receipts, grade notices, balance alerts) crowds out the one mail asked for.
3. **A bare identifier has no meaning to embed.** The live literal path now covers ids Gmail can match at any date;
   ids printed only inside an attachment, and an index that answers ids by itself, are still missing.
4. **Time is guessed.** The extraction call has no Today line; the coordinator prefixes the query with month, day and
   UTC time but no year (`src/infrastructure/agent_coordinator.py:435-436`) while the token asks to "use
   current_date_time" (`prompts_snapshot/tokens/system/EMAILSEARCH_COGNITIVE_PROCESS.groovy:23`). The only date filter
   is the sent date, so "my October flight" booked in July misses an October filter.
5. **Attachments are names only.** Medical and legal mail carry their content in the attachment.
6. **Selection loses mail.** Keep/drop is decided on the snippet; a dropped mail is invisible to topic search.
7. **Smart sees little:** `[id] from | date | files` and the fact cut at 150 characters, no subject, no type
   (`src/infrastructure/delegation_engine.py:218-226`); it compensates by searching again.
8. **Prompt hygiene.** Examples throughout the classifier and search tokens; no language rule; the extraction falls back
   to an empty system prompt (`src/agents/email_search_agent.py:617-618`, against the no-fallback rule); two profile files
   name one agent (`prompts_snapshot/profiles/email_search.yaml`, `emailsearch.yaml`).

**Goals.** G1 a mail is found **exactly** by an identifier, amount, counterparty or person printed in its subject,
body or a read document. G2 a topic is found by the words people use ("trip", "school", "doctor") in the owner's
languages. G3 a time question meets the mail's **event, due or validity date** as well as its sent date, against a real
Today. G4 small results, template mail folded, an honest "nothing". G5 document attachments count. G6 Smart's prompt
does not grow. G7 the stored mail is re-carded from its full body, idempotently, without re-entering memory
consolidation. G8 new mail goes through the same extraction contract online.

**Non-goals.** Indexing all mail (RFC §14.1 stands); topic search in live Gmail (decision record); contact management.
**Decided:** bodies and document text are never stored — only the card and its keys; no staging copy either (§6.2).

---

## 2. The index entry: an email card

One card per indexed mail, written once at index time; a search makes no extraction call. The card is a
**drive profile plus mail fields**: the model writes a `DriveProfile`-shaped answer
(`src/domain/drive_enrichment.py:130-149`) so the drive's pure grading and keying run on it unchanged (§2.2).

### 2.1 Fields

| Group | Fields | Who writes it |
|---|---|---|
| Identity, route | `email_id`, `user_id`, `account_id`, `source`, `from_name`, `from_address`, `from_domain`, `subject`, `sent_at` (= `email_date`) | code (Gmail) |
| Attachments | per file: name, sniffed kind, size, `read` = `text` / `read_by_reader` / `scan_not_read` / `locked` / `skipped:<reason>` | code |
| Profile, automatic | `doc_type` (closed enum), `life_domains` (1–3, closed), `title` (≤ 120), `what_it_is` (the summary, ≤ 300, English, future stated as future), `topics` (≤ 8, and as many native), and the word lists `label_words`, `topic_words`, `domain_words`, `validity_words` in five languages (en, es, fr, uk, ru), ≤ 24 each | model; automatic |
| Profile, graded | `entities` (≤ 12, printed spelling + ≤ 8 forms), `key_values` (≤ 12, `VALUE_LABELS`: booking, order, invoice, policy number, `amount_total`, `amount_due`, `price`, …), `dates` (≤ 5, `DATE_LABELS`) | model; each graded by code as **printed in the mail** or not |
| Time envelope | per date: `label`, `from`, `to` (a range), `printed` source | model + new code (§2.4) |
| About whom | `people[{entry_id?, printed}]` (≤ 6); `about` = `owner` / `household` / `person` / `unknown`, **derived by code** from the surviving people and the model's owner answer (§2.3) | model + code |
| Keys | `about_keys` (the drive keyer's families), `name_keys` (subject and sender name), `people_keys` (`p:<stable entry id>` + printed name forms), `month_keys`, `template_key` | code |
| Vector | `card_vector` (one) | code |
| Lifecycle (v2 only) | `card_state` = `pending` / `carded` / `empty` / `abandoned` / `source_gone`; `card_version`, `people_digest`, `card_model`, `carded_at`, `card_failures` | code |

**Not stored** (no reader in any slice): a separate amounts group (amounts are key values), `to_owner`, `thread_id`,
review flags, drop counters (logged per run instead). Role and currency of an amount and the label of a value are
model-chosen, so automatic; only the printed value can be exact. **v1 fields are never written by v2**: `text`,
`tags`, `category`, the four vectors and v1 `state` keep their writers until cleanup.

**Closed enums.** `doc_type` = the drive's `DOC_TYPES` (`src/domain/drive_labels.py:22-26`) plus mail-only values:
`order_update`, `appointment`, `account_notice`, `message`, `invitation`. Their names (for `lw:` keys) live in a **mail table**, not in
the drive's `DOC_TYPE_NAMES` (`:93`): that table is inside the drive keys digest (`index_tables_digest`,
`src/domain/drive_keys.py:862-870`, via `label_names_digest`), so touching it would start a re-key pass over every drive.
The drive keyer reads type names only from its own table (`drive_keys.py:458`), so the email keyer adds the mail-only
names itself. `life_domains` = the drive's `LIFE_DOMAINS` (`:29-32`). A value outside the set becomes
`other`. *Rejected:* today's 7 categories (they mix kind and area); the memory's 27-domain registry (flag-decided domains
a mail does not have); free tags.

**Languages (decided: five).** The drive's query-side tables and kind names cover en, es, fr, uk, ru
(`drive_labels.py:44-54`), while its profile word lists are four (en, es, uk, ru: `drive_enrichment.py:132`, profiler
token line 33). The mail card writes all five; the keyer is language-agnostic, so nothing else changes.

**Caps: the drive's, unchanged, plus one of the mail's own.** The reused grading and keyer cut every list at the drive's
constants, `src/domain/drive_index_params.py:159-166` (entities 12, forms 8, key values 12, dates 5, topics 8, word lists
24): `drive_profile.py:337`, `:352-374`; `drive_keys.py:446`, `:461-468`. A larger mail cap would be cut silently, and
raising a constant changes the drive keys digest (`drive_keys.py:868`) and re-keys every drive. So the writer is told the
drive's numbers. **Dates 5**, not 6: a range is one date (`from`..`to`), and a booking, a due date and a validity fit in
five. **Word lists 24 across five languages** (the drive's 24 serves four): about four to five words per language with
plurals; if lists prove starved, the cut becomes a parameter of grade and keyer (a drive-module change, drive
behaviour unchanged), never a new constant. **People 6** is new (`MAIL_PEOPLE_MAX` in a mail params module; the drive
has no people field). The card prompt's `{MAX_DATES}` = 5 and `{MAX_WORDS}` = 24 follow.

### 2.2 What is reused from the drive, exactly, and what is new

| Reused unchanged (pure, `src/domain/`) | New code |
|---|---|
| labels: `DOC_TYPES`, `LIFE_DOMAINS`, `VALUE_LABELS`, `DATE_LABELS`, name tables | the mail-only `doc_type` values and their names |
| `drive_profile.grade` (`drive_profile.py:294`): grades every value `in_text` / `in_text_as` / `not_in_text` against the chunks' text and the `name` argument, and drops nothing | the **grade input wrapper**: chunks = the subject, the sender's display name, the body as built (the kept quoted head included; the builder's gap markers excluded), each read attachment's text; `name` = **every listed attachment's name**, one per line (the writer is told names count: a number printed only in a file name is printed in the mail). Code never puts filler (an empty subject placeholder, a marker) into the evidence. |
| the keyer `about_keys_of` (`drive_keys.py:598`) through the `KeyedRecord` protocol (`drive_key_sources.py:65`): a printed value keys `v:`, an unprinted one `va:` (`drive_keys.py:440-449`); a printed entity `e:`, an unprinted spelling `ef:` (`:420-435`) | a card object implementing `KeyedRecord` (kind `email`, photo fields empty) |
| `name_keys_v2` for subject and sender name | `people_keys`, `template_key`, the time envelope reader (§2.4) |

So "verified" means **printed in the mail** (graded `in_text`/`in_text_as`), and an unprinted value is kept but keyed
only in automatic or translation families — never exact. *Rejected:* a separate mail vocabulary and normaliser (two
tables drift; one normaliser serves index and query).

### 2.3 About whom, and people keys

**Stable entry ids (decided; built in slice 0).** Confirmed-people entries carry no id today: `ConfirmedPerson` is name, forms,
relation (`src/domain/confirmed_people.py:31-35`), and the one document is replaced whole on each write
(`ConfirmedPeopleRepository.replace`, `src/ports/confirmed_people_repository.py:21-23`). Its writer is an owner-run script
(source `owner_script`); the review-answer path only reads it (`src/services/profile_question_answers.py:87`); no Cabinet
page edits it. Each entry gets an `entry_id`:
- **Assigned** by a pure `assign_entry_ids(new, stored)` in the same domain module, called by the store itself inside
  `replace`, in one transaction (as built: every writer gets ids for free; ids on the incoming list are ignored, the stored
  document is the authority; only people entries carry an id, never the owner): an entry keeps the stored id of the entry with the same casefolded name (names are unique per list,
  `confirmed_people.py:100-102`), else of the one stored entry sharing one of its forms; otherwise it takes the next id
  from a counter stored on the document (`next_entry_id`; ids are positive integers and are never reused). A rename the owner types keeps the id
  through a shared form; the owner's assignment script (`scripts/people/assign_people_entry_ids.py`) is a dry run by
  default and prints every id kept, new and retired, so a broken link is visible before writing.
- **Carried** by `parse_confirmed_people` / `confirmed_people_document` (`:88-112`) and the Firestore adapter; **never
  rendered** by `render_people_block` (`:146-155`), which the fact profiler's fingerprint hashes
  (`src/domain/fact_profile_fingerprint.py:54`, `:68`): adding ids changes no fact's fingerprint and re-profiles nothing.
- **Migration:** one run of the assignment script gives the stored list its ids (dry run first). Until then cards write
  no `p:` keys (people stay linked by printed name forms only).
- The writer never sees the ids: the static block numbers entries `P1…Pn` per render; code maps a returned handle back
  to its `entry_id` right after the call and drops a handle not on the rendered list.
*Rejected:* keying people by normalised name forms only — a corrected spelling or a new form re-keys silently, two
entries sharing a first name ("Ana") cannot be told apart, and "about my daughter" would match whoever shares a form.
Files touched: `src/domain/confirmed_people.py`, `src/adapters/firestore_confirmed_people_repo.py`, the assignment
script, and their tests. It extends the people store's document by one field per entry plus a counter (the §4.5 store contract of
`FACT_PROFILE_RFC.md`), with no Cabinet change.

**People keys.** A notice from Example School about Jamie's parents' meeting gets `people = [{entry_id: <Jamie's id>,
printed: "Jamie Doe"}]`, keyed `p:<Jamie's id>` and the printed name's `e:` forms. A question "about my daughter" is
resolved **in code** at query time: the relation word against the list (`ConfirmedPeopleRepository.get`, `:16`) gives the
entries carrying it (Jamie, Ana) and their `p:` keys. The card stores no relation word, so a list edit never needs a
re-card to change what "my daughter" finds. *Rejected:* model-written relations (a guessed "son" is worse than none, and
the card would carry memory into the consolidator).

**`about`, derived by code.** The model answers two things: the people the mail is about (each a printed name or
relation, with an entry handle when exactly one entry fits) and whether the owner is one of the subjects (`about`, as
in the prompt draft). Code keeps a person only when a name form is printed in the mail or the printed relation words
point to exactly one entry, then derives the stored value:
- `household` = **the mail concerns two or more members of the household together**: the owner is a subject and at least
  one listed person survives, or two or more listed people survive (a family booking, a school mail naming both
  daughters). What code checks: the count of surviving listed people, which it can verify; the owner's own part is the
  model's answer (automatic) — a mail is "about the owner" by how it addresses its reader, which no printed check proves.
- `person` = exactly one listed person survives and the owner is not a subject.
- `owner` = the owner is a subject and no listed person survives.
- `unknown` = no subject survives (a printed stranger alone counts as `unknown`, kept as a printed name).
A home matter with nobody named (a bill for 12 Oak Street) is `owner` when addressed to the owner; "the house" is found
by the `home` life area, not by `about`. `about` is never a search filter: it is shown on the card and sent to memory
(§6.7); people are searched by their `p:` and name keys.

### 2.4 Time envelope

`sent_at` is when the mail was written; a booking's `event` is when the trip happens, a bill's `due` when it must be
paid, a policy's `valid_until` when it ends. An Albergo Immaginario mail sent 2025-06-02 with booking RM-55120 for 10–14
Aug 2025 carries `event 2025-08-10..2025-08-14`; "my August trip" and "mail from June" both find it. `when` for display:
event, else due, else valid_until, else sent. New code (the drive's `ProfileDate` holds one ISO day, no range), each rule
with tests on invented mail:
- **Yearless dates:** the next occurrence on or after `sent_at` (an `issued` date: the previous one on or before).
- **Ranges** ("10–14 Aug", "10/08 – 14/08/2025"): both ends read from one printed source; the end takes the start's
  month or year when it omits it.
- **Weekday forms** ("Friday 14 March"): the weekday must agree with the date chosen; a disagreement makes the date
  not printed-verified (automatic, never a time filter).
`month_keys` hold `m:YYYY-MM` of every printed time point and of `sent_at`.

### 2.5 Embedding, exact keys, and what Smart sees

**One vector** over a card text: doc-type names · sender name and domain · subject · title · summary · printed entities ·
English topic words. *Dropped:* the tags, metadata and attachment-name vectors (one signal voted three times) and the
v1 fact vector at cleanup. *Rejected:* the raw body head; body chunk vectors (storing the body by another name).
**Decided: only printed keys are exact.** A number the writer did not name stays reachable through the live Gmail key
path at any date. *Rejected:* every printed number as a key (phone and card fragments wholesale; duplicates the live path).

**Per result line** (code-built, inside the untrusted wrapper, ≤ ~350 characters):
`• [id] 2025-06-02 · booking · Albergo Immaginario (albergo.example) · "Prenotazione confermata RM-55120"`
`  → Hotel booking in Rome for 10–14 Aug 2025, 560 EUR. | when: event 10–14 Aug 2025 | 📎 1 read`
`  matched: booking number "RM-55120" (printed); place "Rome" (printed)`

---

## 3. Extraction input and the call

**Body.** The adapter's cleaned text (`src/adapters/gmail_provider_adapter.py:472-517`, HTML through `_html_to_text`,
`:529`). For extraction only, a pure input builder:
- **Links → host** (`Read more (track.example)`): link-heavy bodies spend most characters on URLs. A key printed only
  inside a URL is then not printed.
- **Quoted history shortened, not removed:** the first ~500 characters of the quoted block stay (a reply "yes,
  confirmed" needs what it answers); the rest is the quoted mail's own card.
- **Cap: head 8 000 + tail 2 000 characters**, cut at a sentence or line end; most bodies stay whole. *Rejected:* the drive's 6 000 + 1 500 email rule (`drive_profile.py:211-216`, built for files whose full text
  is also chunked and searched); no cap.
- Plus subject, sender, `sent_at` in the owner's zone, the attachment list with read outcomes, the static block (§3.1),
  and **"owner is a direct recipient"** (an input, not stored): `yes` when one of the owner's addresses is in the
  message's To or Cc header, `no` otherwise (Bcc, a mailing list, a forward). The owner's addresses = the connected
  mailbox's address (the credential's identity, `OAuthCredential.account.label`, set from Google userinfo
  `email`, `src/adapters/google/google_oauth_provider.py:108`, `:118`) plus the mailbox's send-as aliases
  (Gmail `users.settings.sendAs.list`, read once per job; whether `gmail.readonly` allows it is checked live
  first — until then the primary address only). Addresses compare by their address part, casefolded, with a `+tag`
  stripped, and for gmail.com/googlemail.com dots in the local part ignored. Today the listing reads only Subject, From
  and Date (`src/adapters/gmail_provider_adapter.py:184`); To and Cc join `metadataHeaders`, and the full fetch already
  has them.
- The same pieces, without the gap markers, are the grading evidence of §2.2.

**Attachments — the policy:**
1. **Dropped by code, before any download:** inline parts (Content-ID or inline disposition — `EmailPart` has no such
   flag, `src/domain/email.py:66-79`, so the adapter must expose it); `vcf`, `ics`, `p7s`/`p7m`, `pkpass`, `htm`/`html`,
   archives, audio, video; files under 2 KB.
2. **Images are not read** (owner). Listed by name; still openable on request.
3. **Read as text:** PDF, DOCX/DOC/ODT/RTF, XLSX/XLS/CSV (the drive's sheet rule). Kind from the bytes, never the
   declared MIME.
4. **Through the document service, never in process:** `DocumentService.read`, store `email`, a new caller `index`
   (`file_conversion/README.md` §DocumentService.read steps 4–6). The light parser takes **one request at a time per
   instance** (§Parser service), so the job reads attachments at a small fixed concurrency and backs off on busy;
   `PARSER_UNAVAILABLE` is an outage — the mail stays pending, never "no attachment".
5. **Caps:** the 5 MiB parse cap; at most 3 read attachments per mail; per attachment head 6 000 + tail 1 500 characters;
   whole input ≤ 24 000 characters. Identical bytes (sha256) are read once per job. Vendor series are read like any
   other mail: the amount and number that differ are the value.
6. **Scanned PDFs: paid reader, under caps (slice 5, built).** Medical and legal mail often carry their content in a
   scan. A scan goes to the reader when it has at most 10 pages (all or nothing: a longer scan, or one whose page count is
   unknown, is not read) and the page budget allows it: the online path has a per-user daily page cap (knob
   `EMAIL_READER_PAGES_PER_DAY`, `0` turns the reader off online), the scripts take `--max-reader-pages`; otherwise
   `scan_not_read`. A scan read starts only with enough time left before the mail's deadline, and a reader outage gives
   `scan_not_read`, not pending. The page count exists only after the parser, so a read scan is parsed twice (the
   parser-only pass, then the reader pass). Reader text is grading evidence like text-layer text. Reader cost is billed
   by the document service (step 7). No `CARD_VERSION` bump: stored mail whose scans were skipped is re-carded
   selectively, by the stored `scan_not_read` outcome.
   *Rejected:* never reading scans (the medical and legal content stays invisible); no cap.
7. **Password-protected PDFs:** `locked`, no retry, no failure count; openable on request.

### 3.1 The writer's static block: the owner and the confirmed people list only

**Decided for v1:** the card writer sees the owner (name and forms from the confirmed list's `user`, the connected
mailbox address from the credential's identity, `gmail_email_indexing/README.md` § Flows, Connect and reconnect) and the **confirmed people list**
(name, forms, relation, entry id) — nothing from memory. *Why not memory lines:* code checks only entities, values and
dates against the mail; the free-text title and summary are not checkable, so a memory line could ride in them, reach
the consolidator with the whole card and return as "a fact from email". And the baseline cache carries no sensitivity
or profile domain: each cached item holds only id, text, domain, tags, priority, created_at
(`src/adapters/firestore_repo.py:956`, memory README §4.1 item 6), so it cannot be filtered safely. Memory lines are
added only if "about" errors that memory would fix show up in live use — then from current fact profiles with
`sensitivity == ordinary` and allowed areas (`FACT_PROFILE_RFC.md` §2.1), never the baseline cache.

*Where:* one `extra_static_blocks` entry of `build_for_agent` (`include_biographical=False`), after the shared tokens,
before the cache boundary; the mail is the user message. The prefix (shared text, then the account's block) is reused by
every mail of one account in a job. *Rejected:* the fact profiler's placement (list in the user message for one
cross-account prefix): mail work runs per account in long runs.
*Built:* by code from two reads at each job page and online indexing page; its digest goes on the job.
The block lists each entry as `P<n> | name | forms | relation` (per-render handles; the stable ids of §2.3 stay in code).
*How "about me" stays apart from "about a family member":* who received the mail is code's input, never the model's
answer; `owner` only when the subject person is the owner and no listed person is the patient, pupil, passenger,
holder or invitee; a listed person only when one entry fits the printed name, form or a relation only that entry has
(the `FACT_PROFILE_RFC.md` §4.5 rule) — a shared first name or surname is not resolved; code keeps a person only when a
name form is printed in the mail or the printed relation words point to exactly one entry; `about = person` with no
surviving person becomes `unknown`; `about = owner` with a surviving listed subject person becomes `unknown`.
Example: an Example School notice to `alex@example.com`, "Reunião de pais de Jamie Doe, 3 de março de 2026" → `person`,
Jamie's entry; a Clínica Ribeira reminder "Olá Alex, a sua consulta…" with no other name → `owner`.

**One call per mail** (verification and skip are per mail; a hostile mail cannot steer another's card; one failure
costs one mail). JSON schema output, one parse retry, thinking off or low. **Model:** Gemini Flash-Lite, the
cheapest tier that holds the card contract; a tier override per user is the existing agent-tier setting.

---

## 4. Selection for new mail; empty, gone and failed cards

**Decided (owner): a cheap gate stays and leans to keep.** The classifier, batched over the listed page, answers only
keep/drop with a closed reason; it is told a dropped mail is invisible to topic search and a kept one costs one
extraction. The card extraction then runs on kept mail. The gate writes no fact sentence (slice 9b, after memory read the
card, §6.7). *Rejected:* merging keep/drop into the extraction (every listed mail, mostly marketing, would pay a full
read).

**The gate's closed reasons** (code constants, rendered `- value: meaning` into the token):
- keep: `record` (a transaction, order, booking, ticket, bill, statement, policy, contract or official decision
  concerning the owner or the household), `correspondence` (a person writing to the owner, or a reply in the owner's
  conversation), `notice` (a school, health provider, employer, authority, landlord or service provider about the
  owner's own matter or account), `unsure` (the headers and the start of the text do not settle it);
- drop: `promotion`, `newsletter`, `social` (platform activity), `sign_in` (codes, sign-in alerts, resets,
  verification), `automated` (a machine notice with nothing of the owner's own matter).
Two shifts from v1, both from "lean to keep": a payment request is a `record`, and a delivery-progress mail is kept
(it carries an order; the card's `order_update`). *Dropped at build (owner, 2026-11-16):* a `document` keep reason (an
attachment name that shows a document): the gate's input has no attachment names (the listing reads Subject, From and Date
only, `EmailMetadata` carries none), and reading the parts of every listed mail would break the cheap gate; such mail falls
to `unsure`. The reasons and their meanings are code (`src/domain/email_gate.py`), rendered into the prompt as a static
block; the tokens explain the decision and never restate a value.
**Code rule:** an answer that leaves a listed mail out, a reason outside both lists, or a reason that does not belong to
its decision (a drop with a keep reason, or the reverse) **keeps** the mail and logs it; an id not on the page is
ignored; a duplicate id: keep wins (`gate_decisions`; a keep the rule forced is marked `forced` and counted in the
page's `[EmailGate]` line). The gate decides in one shot from headers and the snippet (§6.2): one call in JSON mode, no
tools, the standard parse retry; the v1 body tool is gone. A failed gate call keeps nothing (as v1: the page's mail counts
as failed and the ops alert fires): keeping a failed page would send marketing to the card writer.

**v2-only `card_state`; v1 `state` is never touched.** v1 search filters `state == current`
(`firestore_indexed_email_repo.py:166`), so archiving through it would change v1 results and hide the record from the
re-card scan for good. Instead:
- `empty` (decided: hidden from v2 search, never deleted) — the model's `substance: none`, or no printed entity, value or
  date and a `newsletter`/`other` type. A later `CARD_VERSION` re-reads it, so it can come back.
- `source_gone` — Gmail answers the message is gone (`EmailMessageGone`, `src/domain/exceptions.py:376`); hidden from v2,
  kept for the consolidation trail.
- `abandoned` — three charged failures on one input; retried on the next version.
v2 search reads `card_state == carded` only.

---

## 5. Search v2: the drive's engine, meaning as the base, keys add

### 5.1 Is the drive's engine reused? Yes for its pure core; no for its service and collection

**Reused:** the request becomes concepts by `query_concepts` (`drive_keys.py:748`; numbers first, generic and time words
removed, at most 8); `allowances` (`drive_tiers.py:111`: an absent word blocks and gets the D29 count line; with three or
more concepts the single lowest-weight word may be missed; a word held by too large a share of the cards (the drive's cap) may be missed;
numbers are never missable); `classify` on a card's about and name keys (`drive_tiers.py:292`; with no chunks it
yields About, By translation or Labelled); the meaning z-score with abstention (`source_scores`, `related_floor`,
`:311-352`). These are code-built from the request's own words, not model slots.
**Not reused:** the drive search service and the drive index collection. The repository's streams are connection-scoped
(`find_by_about_keys(scope: ConnectionScope, …)`, `src/ports/drive_index_repository.py:320`; `find_nearest_cards`,
`:221`), the service resolves mounts and folders (`src/services/drive_search_service.py:542-577`) and reads coverage
from drive sync state (drive README §6.3), and rendering is tied to drive records and refs (`src/domain/drive_search.py:31-47`).
Storing mail as drive records would also put it under drive caps, upgrade passes and Cabinet counts.
**New:** a small email card search (two Firestore streams — `about_keys`/`name_keys`/`people_keys`
array-contains-any in batches of 30, and one `card_vector` kNN), the mail generic words ("email", "mail", "correo",
"лист", "письмо", "courriel", …) added to the removed words, the U merge (§5.3), time windows, folding and an email renderer.

### 5.2 Slots: one small call, used to add, not to filter

The ECO extraction (`src/domain/user.py:130`; Gemini, `src/services/agent_context_builder.py:119-124`) becomes slots:
`subject` (English phrase for meaning), `doc_types[]`, `domains[]`, `people[]` (name or relation), `time {intent, from,
to}`, `exact_keys[]` (as today), `scope`. Inputs: the request, the last 3 turns, `Today: <date> (<weekday>), timezone
<zone>` in the user message. Code validates enums and dates; a failed call degrades to concepts + meaning (no prompt);
the empty-prompt fallback goes. Relation words in `people` are resolved in code against the confirmed list (§2.3).

### 5.3 The U rule (a union, not an intersection)

Strict AND over model-filled slots fails in practice: an inferred key that no card holds empties the whole set (the
memory list mode hit this). So:
1. **Base:** the meaning list — card kNN on `subject`, top 50, scored by the robust z; it abstains on a small or flat pool.
2. **Add and re-rank, never remove:** drive tiers over the request's concepts (About → By translation → Labelled);
   exact-key hits (`v:` / `n:` of `exact_keys`, which code checks occur in the request text); `people_keys`; doc-type and
   domain label keys; time-window members. A candidate's rank = its best tier, then the number of slot lists that found
   it, then meaning z, then newest `when`.
3. **Time is a filter only when the request's own text names a time** — a year, a month name, a date or a drive
   `TIME_WORDS` phrase (`drive_keys.py:129`), checked by code; otherwise the slot only re-ranks. Window semantics:
   `period`/`past` — any time point or `sent_at` in the window (`month_keys`, days checked in code); `upcoming` — an
   `event`, `due` or `valid_until` point from today on; `latest` — order only.
4. **Honest nothing:** no tier match, no exact hit and the meaning base abstains → "No indexed mail matches." A request
   word no card holds gets the D29 line (`No indexed mail names "Ryanair". Without it: 3 mails match the rest.`).
*Rejected:* strict AND across model slots (inferred keys empty the set); RRF over kNN lists; a fixed cosine threshold; a judge.

### 5.4 Size, folding, presentation

Shown: tiers 10 + 8, meaning-only lines 5, "showing n of N"; `scope=all`: every tier member up to 50 lines. **Folding by
code fields only:** `template_key` = sender domain + the subject with digits, dates and amounts masked; a group shows its
newest member in scope plus `+ 11 more of the same kind from bank.example, Jan 2025 – Oct 2026`. **Only an exact id or
amount match stops a member from folding** (a sender or word match never does, or templates would never fold). Order of
the result: index lines, then fresh headers, then per-key Gmail matches; an id shown earlier is dropped later
(`email_search_agent.py:457-524`). Reading lines (code texts, a `*_texts.py`, as the drive's `R_*`): the index's reach,
"re-carding in progress: N mails still have old entries", the D29 lines, "No other indexed mail matches all of these."
Smart's prompt is unchanged.

---

## 6. Re-card of the stored ids

### 6.1 Skip before any fetch

The scan reads every stored record (any v1 `state`, any `card_state` but `source_gone`) by projection and skips, **before
any Gmail fetch, parse or paid read**, a record whose stored `card_version` is current, `card_state` is `carded` or
`empty`, and `people_digest` — sha256 over (`entry_id`, name, forms) of the entries its card resolved plus the entries whose
forms match a printed name on the card — equals the same digest over today's list. The input fingerprint is computed only after the
fetch and is stored for audit, never used to skip. **Every `CARD_VERSION` bump re-buys the paid reading of scans**
(bodies and document text are not stored); the bump's price is the reader pages of every scanned mail
on record (§Decided: this cost is accepted on a writer change). A list edit re-cards only the mail naming the edited person.

### 6.2 Execution shape

**The re-card of stored mail runs online** (a one-off, small enough for a bounded job): one extraction call per
mail at a fixed concurrency, an enforced `--max-usd` and `--max-reader-pages` (hard stop in the job, not a warning), the
job touching its `updated_at` on every page (the email watchdog fails `running` jobs idle 2 h, `worker_handler.py:451`).
The input text lives in memory only; bodies are never stored.

**Batch is for the initial backfill of a very large new mailbox** and for large `CARD_VERSION`
re-cards — a later slice, built when that client arrives, through a small caller-driven batch executor extracted from
the transcripts' lifecycle rules. Constraints it puts on this design now:
- the gate must decide in **one shot** from headers and the snippet (today's classifier may fetch a body over up to 4
  turns, which does not batch);
- a card request is self-contained (one call, no parse-retry round in batch: an invalid answer is retried online);
- no staging copy of bodies: the consume step re-fetches the mail from Gmail and checks the input fingerprint; a
  changed input is re-queued, never graded against a different text;
- embeddings stay online, for kept mail only.

### 6.3 Consume, CAS, idempotence

Parse → grade against the input text → time envelope, keys, `template_key`, card text (pure) → embed → **field update**
of v2 fields only, under compare-and-set on the `card_version`/`people_digest` read at selection
(`FACT_PROFILE_RFC.md` §2.3). A duplicate result writes once; a crashed job resumes from its cursor. Failures are counted
per input; three → `abandoned`. A provider outage is not a failure. Dollar and reader-page caps are enforced flags.
`KEYS_VERSION` (the shared normaliser) re-keys from stored card fields, no model call; an embedding-space change
re-embeds stored card text.

### 6.4 `consolidated_at` and the v1 writers

The v2 write never touches `consolidated_at`, so re-carded mail is never re-sent (`get_unconsolidated_batch`,
`firestore_indexed_email_repo.py:338-347`). **The v1 `reindex` mode is disabled in the first card slice:** it re-saves
whole documents (`email_indexing_service.py:320-323`, `:499`, `:511` → `save_batch`'s `batch.set`,
`firestore_indexed_email_repo.py:131`), which would wipe every card. Incremental and backfill already skip stored ids.

### 6.5 Online path, rollback, cut-over

New mail: gate → input builder → online extraction → grading and keys → one save with v1 fields, card fields and
`consolidated_at = None`; an extraction that cannot run now leaves `card_state = pending`. v1 fields stay until cleanup;
v2 search is the only search (the `EMAIL_SEARCH_V2` switch was removed by slice 10, §6.8); there is no v1 fallback. *Rejected:* a new collection (doubles the consolidation
marks, skip logic, repair job and counts); today's `reindex` mode (re-lists and re-classifies the mailbox).

### 6.6 Order

Card, attachments and extraction first; then the store and the online path; then the re-card; then the search switch
(§9 lists the slices in order). The search switch ships with no labelled evaluation (§8).

### 6.7 Memory consolidation of new mail: the whole card, through its own filter

In v1, Stage 3 email triage (batches of 100) sent the classifier's fact sentence, category, tags, date, sender, subject,
attachment names and metadata; slice 9 replaced that with the card and slice 9b removed it (`_consolidate_email_cards`).
- **What it receives (owner: the whole card), through a code filter:** `email_id`; `date` = `sent_at` in the owner's zone
  (still copied into `reported_date`, `:1262`); sender; subject; doc type; life areas; title; summary; the time envelope;
  the entities, values and dates **printed in the mail** (unprinted ones are not sent); attachments with read outcomes;
  `about` and people as printed names only (no entry ids, no relations — they came from the owner's list, sending them
  back would be an echo). The filter withholds both title and summary (free text from the same writer) when the summary names a list person not
  printed in the mail, and on every card when the people list is unreadable. Only verified time points are sent, and an
  attachment is sent as a name and whether it was read. Keys,
  vectors, word lists and lifecycle fields are not sent.
- **Waits for the card (slice 9):** selection takes `consolidated_at == None` and `card_state` settled, through one
  composite index with `card_state`. Nothing retries a `pending` card online, so unsettled mail (`pending`, or no card
  fields) enters once its `indexed_at` is older than a fixed wait (`MEMORY_CARD_WAIT`, 6 hours); strict waiting would
  starve it.
- **Mail with no readable card (slice 9b, owner 2026-11-16):** `carded` with a readable card goes as the card; `empty` is
  skipped (marked consolidated, never sent: the card writer found nothing of substance, and lean-to-keep makes these
  many); `abandoned`, `source_gone`, unsettled past the wait and an unreadable `carded` card go as a **header**
  candidate: `{email_id, date, from, subject}`, plus `attachments: [{name, read: false}]` when names are stored. No
  snippet: it is a glimpse of untrusted text (§1.1). A pass whose mail is all skipped makes no model call. Old mail that
  still stores the v1 fact goes as its header too; the fact is never read. *Rejected:* headers for `empty` too (feeds
  newsletter subjects to the PERFORMANCE tier).
- **The token explains the card:** a new consolidation token for email candidates replaces the inline framing text
  (`consolidation_agent.py:1259-1263`): the card is model-written from someone else's mail, not the owner's words;
  which fields are printed in the mail; `date` is when it was sent, event, due and validity dates are when things
  happen; `about` says whose fact it is; an unread attachment means unknown content. Existing rules apply
  (`CONSOLIDATION_LIFECYCLE.groovy:55`).
- **Re-carded mail never re-enters** (§6.4), pinned by a test. A stored mail never consolidated enters once, as a card.
- **Cost rises per new mail** (a card is several times a sentence), so the card path has its own, smaller batch size.
- **No switch (slice 9b):** the card read is always on; the slice 9 switch `EMAIL_CARD_MEMORY`, its deploy key and the
  dry run's compare mode (card form against the one-line form) are removed with the one-line form. The v1 branch of
  Stage 3, whose message claimed the system's voice (`[system_alert]`), is gone.
- **The gate's fact is gone (slice 9b):** the classifier keeps by its decision, not by having written a sentence; the
  stored record loses `text`, `tags`, `category` and `metadata` (old documents keep them, unread; no migration).

### 6.8 The search switch (slice 8)

The v2 search was built behind the optional knob `EMAIL_SEARCH_V2` so the owner's live use could judge it alone; slice 10 removed the knob, the deploy key `EMAIL_SEARCH_V2_DEV` and the v1 path (no rollback). The container now builds one `EmailCardSearchService` over `FirestoreEmailCardIndex` unconditionally and hands it to `EmailSearchAgent`, which requires it; a v2 error fails the search. As-built deltas of slice 8:
- the slots call stays inside `EmailSearchAgent` on its ECO Gemini context, with its own profile `email_query_slots`, no new
  agent class (the `email_search` profile of the v1 search was deleted by slice 10);
- the coordinator's delegation timestamp is stripped from the query before slots and concepts, or every request would turn
  into a one-day list for today;
- the slots prompt gets no biographical facts, directives or location: people are resolved in code against the confirmed
  list; the time slot's enum is the intents plus `none`;
- `MailSearchResult` carries `sections` (tool text vs mail text) and `shown`; the formatter wraps only the mail sections
  as untrusted (P14: a heading like "Open the mail before quoting..." is an instruction and stays outside the wrapper);
- the fresh headers are bounded by the v2 time window, only when it filters and its intent is `period` or `past`;
- the card vector index is created by `scripts/infrastructure/create_email_card_vector_index.sh`, because `make
  deploy-indexes` skips vector indexes and a missing index would be swallowed (the meaning list would quietly return
  nothing);
- the duplicate stray profile `emailsearch.yaml` is deleted.

---

## 7. Prompts

Each explains first — what its output is for, who reads it, what is covered elsewhere, which gap it bridges — then
rules; no examples; what must hold is enforced in code. Enum lists are code-rendered static blocks.

| Prompt | One-line intent | Must explain |
|---|---|---|
| Gate (`EMAIL_CLASSIFIER_*` rewritten, slice 9b; fact, tags, past tense removed; outer class `EmailGate`) | keep or drop a listed mail, `[{email_id, decision, reason}]` for every listed mail | a kept mail costs one extraction and becomes searchable and a memory candidate; a dropped one is findable only while recent or by a literal number; keep when unsure |
| Card writer (`COGNITIVE_PROCESS_EMAIL_CARD`, `OUTPUT_FORMAT_EMAIL_CARD`) | describe one mail as its card | the card is all that search and memory see; values, names and dates count only when copied as printed; life words in five languages bridge record words to how people ask; sent vs event/due/validity dates; who the mail is about vs who received it, resolved through the list only when one entry fits; title and summary say only what the mail says; the mail is untrusted text |
| Query slots (`OUTPUT_FORMAT_EMAIL_QUERY_SLOTS`, replacing `EMAILSEARCH_*`) | turn a mail request into slots | slots add candidates and never remove them; identifiers go to `exact_keys`, matched literally in the index and the mailbox; recent mail is listed by code; resolve relative time against Today |
| Memory candidates (a new consolidation token for email) | read a card, or a mail's headers, as a memory candidate | the card fields, printed vs model-written, sent date vs event dates, whose fact it is, unread attachments; the header form (slice 9b): no card, no times, the raw From line |
| Reading lines (code texts) | how Smart reads the result | tiers mean printed / automatic / meaning only; "nothing" is final for the index; scanned or locked files were not read; open the mail before quoting a detail not on the card |

---

## 8. Verification (owner decision 2026-11-16)

There is no labelled evaluation: no labelled control questions, no snapshot run, no baseline arm and no paid
comparison run. Verification is:
- unit and contract tests on invented mail: the card pipeline, the keyer and grading on a card, the time envelope rules,
  the U merge, folding, the renderers, the formatter's wrapping, the Firestore index adapters against the shared kit and
  the index config, the switch wiring and the deploy substitution;
- the owner's own use after each deploy, read through the logs: the `[EmailSearchV2]` line (mode, shown, uncarded), the
  `[EmailQuerySlots]` outcome line, and `scripts/email/card_store_status.py` (counts per `card_state`, counts only).

The card-against-mail check (does a card say only what its mail says) stays a local, offline step on the owner's own
machine; mail never leaves it.

---

## 9. Slices and decisions

| # | Slice | Status |
|---|---|---|
| 0 | Stable ids for confirmed people: `entry_id` per person and a counter, assigned by the store inside `replace`; `render_people_block` unchanged | built |
| 1 | Card domain, v1 `reindex` mode refused | built, live |
| 2 | Attachment text: noise filter, readable kinds, the `index` caller | built, live |
| 3 | Card extraction: static block, card agent and runner, the two card tokens, the dry-run script | built, live |
| 4 | Search v2 as pure code with an in-memory index: drive core, two streams, U merge, folding, renderer | built, live |
| 5 | Scanned PDFs through the paid reader under per-mail and daily page caps | built |
| 6 | Store and online path: field updates with compare-and-set, `card_state`, indexes | built, live |
| 7 | Re-card job: skip-before-fetch scan, online execution, enforced caps | built, live |
| 8 | Search switch: `EmailCardSearchService` on `FirestoreEmailCardIndex`, slots call, formatter sections (§6.8) | built, live; the switch was removed by slice 10 |
| 9 | Memory feed: the filtered card to Stage 3, the waiting rule and its index, the new token (§6.7) | built, live; its switch was removed by slice 9b |
| 9b | The gate of §4 (closed reasons, the lean-to-keep code rule, one shot in JSON mode, no body tool, `document` dropped); no fact, tags, category or metadata stored; Stage 3 skips `empty` and sends the headers of other uncarded mail; `EMAIL_CARD_MEMORY` and the compare mode removed | built (owner decision 2026-11-16: no rollback, no migration) |
| 10 | Cleanup: the four v1 vectors, their indexes, the v1 search, the embedding repair and the search switch removed; the v1 fields something still read (`text`, `tags`, `category`, `metadata`) stayed until 9b | done (owner decision 2026-11-16: no rollback). Owner steps: delete the repair scheduler job, the eight live v1 vector indexes and the `email_search` prompt documents; optionally run `scripts/email/drop_v1_vectors.py` |

**Decided** (owner, 2026-11-16 unless noted)
1. Scanned documents: their text is not kept, so each change of the card writer re-buys their reading; accepted.
2. Memory gets the whole (filtered) card, not a shorter one.
3. Stable ids for confirmed people are done now, not after the memory observation ended.
4. The old index is removed right after the switch, with no rollback (slice 10).
5. No labelling: no expected-mail list and no sample fields are labelled (§8).
