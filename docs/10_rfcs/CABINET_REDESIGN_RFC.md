# Cabinet redesign RFC

**Status:** Proposed (2026-11-23). Owner-approved direction and mockups; this document is the build spec.
**Owner's map:** [user_cabinet](../05_building_blocks/user_cabinet/README.md). Facts PIN behaviour:
[CABINET_DEVICE_SESSIONS_RFC](CABINET_DEVICE_SESSIONS_RFC.md) §7.7 (unchanged here).

## 1. Why

The Cabinet grew one feature at a time: each feature added a card with its own inline styles to one page
(`src/web/static/cabinet.html`, ~7 100 lines, ~250 inline `style=` attributes). Every part works; the whole is hard to
use:

- **Navigation mixes two models.** Seven items: five switch sections in the page, two (Call, Modes) leave for other
  pages. The floating header sits over the content, so scrolled text shows through it. The address bar does not follow
  the open section.
- **Settings is a pile.** Timezone, location, language, voice languages, deep research, bot reminders (a feature, not a
  setting) and the facts PIN, each a card of a different width with its own Save button.
- **Security is in three places:** the PIN in Settings, passkeys and devices in Devices, the PIN prompt in Memory.
- **The PIN card shows every operation at once:** change needs five fields (current PIN, new PIN twice, duress code
  twice) wrapping onto two lines, then a second "current PIN" field for removal, then "forgot". The duress code gets
  one sentence in the card's description, yet the server requires it on every set and change.
- **Memory filters are 28 chips** (27 life areas plus "All"), with no counts and empty areas shown.

The drive section is the exception: it was designed as a unit (collapsed rows that open on click, one component
family `dv-*`), and the owner finds it the one comfortable part. It is the model for the rest.

## 2. Goals, non-goals, anti-goals

**Goals.**
- One information architecture ordered by use: the owner opens the Cabinet to call Lelik or to look at memory, then
  drives and settings, then reminders.
- One component system and one stylesheet, so a new feature is assembled from existing parts and cannot bring its
  own look.
- A home page, opened by default on a phone and a computer: the call button and one large tile per section, each
  tile carrying a one-line status that turns into a warning when something there needs the owner.
- Every section shows a short list first; detail opens on click (the drive pattern).
- The Cabinet's own brand sheet (§3): tokens, components, rules.

**Non-goals.**
- The call and modes pages (`call.html`, `call_modes.html`). They are installed as separate Home Screen apps and stay
  clean screens with no Cabinet header. Untouched.
- The landing page. Its readers are not Cabinet users, so it keeps its own showcase look; the two styles differ on
  purpose and no alignment is planned.
- Any server behaviour change, except the one counts route of §5.2.
- New features. Every operation the Cabinet has today stays, in its new place (a move is a move).

**Anti-goals** (a failure even if everything works):
- A section that again shows all of its forms at once.
- A stylesheet per feature, or inline styles in new markup.
- A framework, a bundler or a build step for one page.

## 3. Brand sheet

The Cabinet keeps its current look (light and dark by system setting, system UI font, blue accent) and makes it one
system. The landing page is a separate showcase style and is not part of this sheet.

- **Mark:** the robot avatar with the wordmark "Alek", as today.

**Tokens** (light; dark values redefine the same names under `prefers-color-scheme: dark`, as today): ground
`#f2f2f7`, group `#ffffff`, text `#1d1d1f`, secondary text `#6e6e73`, separator `rgba(60,60,67,.12)`, accent
`#0071e3` (dark `#0a84ff`), link text `#0066cc`,
success `#34c759` / text `#1f7a35`, warning `#ff9500` / text `#9a4500`, danger `#ff3b30` / text `#c5000f`. Spacing
4 / 8 / 12 / 16 / 24 / 32 px. Radii 10 / 12 / 16 px; buttons fully round. Type: section title 34/700, form step
18/600, row title 16/500, value and explanation 15 and 14 regular in secondary text, group label 13/600 uppercase.

**Components** (one definition each, in the shared stylesheet): page header; group label; group (a white rounded
block of rows); row (title, value or status, chevron; opens a body); row body; field (label above input); buttons
(primary, neutral, dark for search, danger, link); status chip (works, off, needs attention, error); segmented
control (up to four options); switch; select with counts (more than four options); explanatory box (for an unusual
or risky step); stepper header ("Step 1 of 2"); modal; toast.

**Rules:**
1. One column, 760 px wide, for every section.
2. The row is the unit: title and current value; forms and detail open on click.
3. One action at a time: a form shows only the current step; long actions are split into steps.
4. Switches and selects save at once; a Save button only where text is typed.

## 4. Information architecture

**Menu (desktop header and phone bottom bar, same five items):**

| Item | Content |
|---|---|
| Home | The default page (§5.0): call button, section tiles with status |
| Memory | Facts, search, filters, review questions; PIN prompt when locked |
| Drives | The drive section, as today |
| Reminders | Bot reminders (moved out of Settings) |
| Settings | Everything else, in groups (§5.1) |

The header is a solid bar at the top (sticky, opaque), no longer a floating pill over the content. The account email
and Log out move from the header into Settings → Account. Call leaves the menu and becomes the large button on Home;
Modes leave the menu and become a link under that button and a row in Settings → Voice. The call pages themselves are
untouched: the Home Screen call app opens its own address, not the Cabinet.

**Old sections → new places:** Integrations → Drives (drive list) and Settings → Connections (Telegram, Slack, phone,
Gmail, Microsoft To Do). Devices → Settings → Security. Team → Settings → More (owner only, as today). Settings'
reminders → Reminders.

**Addresses.** The open section is written to the address (`?tab=<section>`, `history.replaceState`), so reload and
back keep the place. Default section: Home, on a phone and a computer alike; the phone redirect of a bare `/cabinet`
to the call page is removed (its job, a fast way to the call, is the Home button). A connect
return (`?connected=<kind>` or `?connect_error=…&kind=…`) opens the place of that connector whatever `tab` says:
Drives for a drive, Settings → Connections with the row open for anything else. Old values keep working as aliases: `integrations` opens Drives, except
when the return parameters belong to a Settings connection (Gmail, To Do, a platform), which opens Settings with that
row open; `devices` opens Settings → Security; `team` opens Settings → More. The call pages' "← Cabinet" link
(`?tab=integrations`) and the bot's memory link (`?tab=memory`, `deploy_topology.py`) keep working through the
aliases, so neither changes.

## 5. Sections

### 5.0 Home

- **Call:** a full-width button "Call Lelik" opening `/cabinet/call`; under it a link to the modes page.
- **Tiles** (two columns on every width, large touch targets), each opening its section, each with one status line
  built only from what the existing routes already return, read in parallel on load:
  - **Memory:** "closed with PIN" when locked; otherwise the number of review questions waiting, or a plain "open".
    No facts and no fact counts on Home, so Home never goes around the PIN.
  - **Drives:** the number of drives and "all working", or the first problem (reconnect needed, paused, error) as a
    warning.
  - **Reminders:** the next reminder's time and label, or "none scheduled".
  - **Settings:** "connections working", or the first connection needing action (Gmail, To Do, a platform) as a
    warning.
- A warning tile gets the warning colour and border; its status line says what is wrong in a few words. A failed
  status read shows a neutral line on that tile, never an error page.

### 5.1 Settings

Groups and rows, each row collapsed to title and current value:

- **Account:** email (Google) → Log out.
- **Profile:** Timezone (select, saves at once) · City (text, Save) · Interface language (select, at once) · Reply
  language (segmented: as I write / always in the interface language, at once) · Voice languages (multi-select pills,
  at once). The two language rows share one stored pair (`preferred_language` + `agent_mirror`, one
  `POST /api/user/language`): each row sends both fields.
- **Connections:** Telegram, Slack, phone (one row, status "n of 3"; body lists each with connect/disconnect) · Gmail
  (status; body: daily review, index actions, disconnect, delete data) · Microsoft To Do (status; connect, reconnect,
  disconnect) · Drives (link to the Drives section).
- **Security:** Memory PIN (§5.3) · Face ID and passkeys (list, remove, add on this device) · Devices (sessions, log
  out one, log out others, log out all) · If a phone is lost (the three steps).
- **Voice:** Call modes ↗ (opens the modes page).
- **More:** Deep research two-pass (switch, at once) · Team (owner only: invite link, codes list).

Every operation of the old Integrations, Devices, Team and Settings sections is present in one of these bodies.

### 5.2 Memory

- PIN prompt as today when locked (the PIN exists to guard this section).
- Review questions collapse into one row at the top ("Alek has n questions about your memory"), opening to the
  current question UI.
- Search field and a dark Search button.
- State: segmented control (current / stale / closed).
- Life area: one select-with-counts button ("All areas · <total>"). Its list shows only areas with facts in the
  chosen state, sorted by count, with a filter field; a footer says how many empty areas are hidden. A fact can sit in
  several areas (the filter matches `domain_keys`) and an unprofiled fact in none, so area counts do not add up to the
  total; the list says so in one line. Hidden for "closed", as the chips are today.
- Facts: rows (text, area tag, date) instead of the bento cards, on every list (current, stale, search results). A
  row opens to the two actions a card has today: Edit (the modal that copies a correction message for chat) and
  Delete (invalidate). Closed facts stay read-only rows, as today.

**New route** (the only server change): `GET /api/user/facts/domain-counts?state=current|stale` → `{total: n, counts:
{<domain key>: n}}` (areas with zero omitted), one Firestore count aggregation for the total and one per domain, in
parallel, account-scoped, matching the browse filter (`domain_keys`), behind the facts unlock guard (counts are facts
metadata, so a locked memory does not leak them). Pinned in the URL map test.

### 5.3 Memory PIN (Settings → Security)

The row shows the state (off / on / resetting). Its body shows one thing at a time:

- **Off:** what the PIN does (Memory asks for four digits before showing facts; open for 15 minutes or until you
  leave the section) → Set PIN.
- **Set:** step 1 of 2, new PIN twice → step 2 of 2, duress code twice, with an explanatory box: if someone forces you
  to open memory, type this code instead; the account locks exactly as after five wrong PINs, so nobody can see you
  typed it. One `POST /set` at the end.
- **On:** Change PIN · Remove · Forgot PIN.
- **Change:** step 1 of 3 current PIN → step 2 new PIN twice → step 3 duress code twice (the server requires it on
  every change; the text says it may be the same code or a new one). One `POST /change` at the end; a wrong current
  PIN returns to step 1 with the error and attempts left, the new codes cleared.
- **Remove:** current PIN → Remove (danger).
- **Forgot:** the 24-hour reset explained → Request reset; then the pending state with Cancel reset.
- After a change or reset request, the existing "log out other devices?" offer.

The API (`facts_pin_app.py`) does not change.

### 5.4 Drives

Content and behaviour unchanged. In the last step its `dv-*` rules give way to the shared components where they are
the same thing (row, group, chip, switch, buttons); drive-only parts (stats, meter, picker) keep their own rules.

### 5.5 Reminders

Its own section: the list as rows (label, next run, repeat), a row opens to edit and delete; "+ New" opens the form
in place. Same operations as today.

## 6. Code shape

- `cabinet.html` becomes the shell: header, menu, section containers.
- `cabinet.css`: tokens and components (§3), the only stylesheet.
- One script per section (`cabinet-memory.js`, `cabinet-settings.js`, …), one for the shell and navigation, one for
  the strings of the four languages. Plain scripts, no modules or build.
- Served by one route `/cabinet/assets/<name>` over a fixed allowlist (the main app has no static folder), with the
  same revalidate-on-load caching as the page. Pinned in the URL map test.
- Tests that read `cabinet.html` as text (28 files) read the whole Cabinet source through one helper that joins the
  shell and its assets; switching them to it is a mechanical change.

## 6a. No operation is lost

The main risk of this redesign is silent loss: a card with three actions becomes a row with two. It is guarded in
code before anything moves:

- **Inventory first.** Before step 1 changes anything, one test file pins today's Cabinet: every user operation
  (each control's handler, from `onclick`, `onchange`, `onkeydown` and `addEventListener`; today about 70 distinct
  handlers) and every server call the page makes (each `${API}/…` path with its method; today 38 paths), as an
  explicit list. Written and green against the current page, on `main`, in the first commit of step 1.
- **The list only changes by review.** A step that renames or merges an operation edits that list in a
  `test(mechanical)` commit (old name → new name); a step that drops one is a red test that goes to the reviewer under
  the test rule. No step may shorten the list on its own.
- **Each PR body** carries a "moved" table for its section: old place → new place for every operation it touches.
- The owner's hand check of each step uses that table.

## 7. Approaches considered

- **A. Restyle section by section inside the current file.** Small steps, but the 7 100-line file and the old styles
  stay until the very end, and each step fights the previous look.
- **B. Shell and shared stylesheet first, then sections in order of use (chosen).** The first step lands the brand,
  the menu, addresses and the file split; every later step moves one section onto it. Each step ships on its own.
- **C. Rewrite in a UI framework.** Rejected: a build step and dependencies for one page of a solo project, with no
  behaviour it would enable.

## 8. Steps

1. Shell: the operations inventory (§6a) first; `cabinet.css` with tokens and components, the new header and phone
   bar with the final five items, addresses and aliases, the asset route, the test helper; Home as a first version
   (call button, modes link, tiles without status lines); the other sections keep their content and move under the
   new menu as they are (Integrations content shown under Drives and Settings, the reminders card under Reminders,
   Devices and Team under Settings). The phone redirect and today's default (now Drives, through the `integrations`
   alias) stay until step 2.
2. Home's status lines (§5.0); Home becomes the default and the phone redirect goes.
3. Settings: groups and rows; Integrations (non-drive), Devices and Team move in; the PIN flow of §5.3.
4. Memory: segmented state, area select with counts (new route), fact rows, collapsed review questions.
5. Reminders as its own section.
6. Drives onto the shared components; old styles and inline styles removed; the owner's map updated with the
   component list and rules.

## 9. Left to the builder

Exact copy in four languages (new strings get all four), the order of rows inside a group, icons, motion, the phone
layout of the select-with-counts list (a bottom sheet is fine).
