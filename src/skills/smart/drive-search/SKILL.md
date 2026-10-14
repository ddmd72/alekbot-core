---
name: drive-search
description: "Use before the first search of the user's drive in a conversation (search_files_in_drive), and whenever you answer from the user's own documents."
---
# Searching the user's drive

The drive holds the user's own files: documents, scans, photos, notes. The index describes every file (what it is, its dates, places, people, values) and keeps its text, so a search usually tells you enough to answer without opening anything.

## First decide: find, or answer from inside a file

- **"Find", "show me", "which files", "everything about X", "do I have …"** — the user wants the files. Answer from the search results alone: each result already says what the file is, where it is, its date and why it matched. Do not open files to confirm them.
- **The user wants something written inside a file** — an amount, a term, a date, a quote, a total, what a specific document says — then open that file. Open only the files the answer needs.
- A passage is a window cut from the file around what matched. Before quoting more than it shows, open the file. If the file cannot be opened (HEIC, an image over 5 MB, a PDF over 20 MB), quote the passage and say it is an excerpt.
- **Many results**: give the count, group them (by folder, type or date) and name the few most relevant; offer to narrow down. Never open them one by one to describe them.
- **Photos**: answer from the description, place and date in the result. Open a photo only when the user wants to see it or asks about something the description does not cover.

## Writing the search

Search in the user's own words; the index already matches other spellings and word forms. Put a time span in the date fields and a file format in `drive_kind`, not in the words. One search per subject is usually enough: search again only to follow a lead the results give (another spelling the results show, a folder, a word to drop), not to re-check what was found.

## Reading the results

Drive results say how each file matched. 'About' and 'mentioned in' are exact: the file's own name, details, place or text holds the words, in their spellings and word forms. 'By translation' and 'labelled' are the index's automatic words — passage keywords with their source quote, type, topic and life-area words — and may be wrong or incomplete. 'Related by meaning only' lines are guesses. Other spellings and word forms were searched; other languages only where the index wrote them, so a search in another language can still find more. The coverage line says what the search could not see. When a word is held by no file, the result counts the files that match the other words, so you can tell whether the word was phrasing or the subject.

- Trust the exact tiers. When the answer depends on an automatic one, check its quoted source or open the file.
- Mention "related by meaning only" lines only when they help, never as findings.
- "Coverage: complete" makes a not-found reliable.

## When nothing is found

- While the index is still being built or rebuilt, never say a document does not exist: say the index is not complete yet and offer to list the folder.
- When the coverage line names what the search could not see (documents not read yet or not described yet, a truncated card, only names and metadata searched), a not-found says what was not searched (e.g. '3 documents are not read yet') and offers to list or open the folder.
- When a folder card is shown under About (its name holds the words), search within it (`drive_folder`) or list it before answering that nothing was found.

## Answering from documents

- **Numbers**: search passages are windows, not whole tables. For sums, totals and comparisons, open the file, then compute from the opened text.
- **Dates**: If a date-filtered search does not show the file asked for, search again without the filter.
- **Freshness**: results show the drive as of the time they state. If the user refers to a recent edit, or a result is marked stale, open the file (it is read live) before quoting it.
- **Versions**: when several results look like versions of one document and their values differ, never pick silently: answer from the version its name or date marks as latest, say which, and list the others with their dates.
- **Document vs memory**: a document is authoritative for its own contents (its terms, amounts, dates). It does not override memory about the user's current situation: when they disagree, give both and cite the document's path and date.
