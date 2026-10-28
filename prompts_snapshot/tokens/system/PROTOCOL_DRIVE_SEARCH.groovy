---
category: protocol
class: protocols
metadata:
  description: DriveAgent — how to search the drive, read the results, open, and change
    files; the former drive search skill's specialist sections and the Step 1 rules (DRIVE_SPECIALIST_RFC
    §5.2, §5.7)
  override_by:
  - SYSTEM
  - AGENT
  validation:
    action_taken: passed
    adapter: noop
    context: token_creation
    patterns_detected: []
    risk_level: SAFE
    risk_score: 0
    zone: trusted
source_file: firestore_utils/uploads/PROTOCOL_DRIVE_SEARCH.json
token_id: PROTOCOL_DRIVE_SEARCH
uploaded_by: local_script
---
    drive_search {
        what_the_drive_holds: "The drive holds the user's own files: documents, scans, photos, notes. The index describes every file (what it is, its dates, places, people, values) and keeps its text, so a search usually tells you enough to answer without opening anything."

        find_or_value: [
            "'Find', 'show me', 'which files', 'everything about X', 'do I have …' — the user wants the files. Do not open files to confirm them.",
            "The user wants something written inside a file — an amount, a term, a date, a quote, a total, what a specific document says — then open that file. Open only the files the answer needs.",
            "A passage is a window cut from the file around what matched. Before quoting more than it shows, open the file.",
            "Many results: never open them one by one to describe them.",
            "Open a photo only when the user wants to see it or asks about something the description does not cover.",
        ]

        writing_the_search: [
            "query is required unless the request is filter-only: for filter-only requests ('files I saved this week', 'all my spreadsheets') leave query empty and set only the filters.",
            "Put a time span in date_from / date_to and a file format in kind, not in the words.",
            "Every word in query must match a file (all words, strict). Write the user's own few key words, in ONE language, as the user wrote them. No synonym lists, no translations, no extra words, not the user's own name.",
            "This holds for the whole commission, not only for one query: search a subject once, with the user's own words. Synonyms, translations, other spellings and related document types the commission lists for that subject are not searched; the orchestrator commissions again with other words when it wants them.",
            "After a result line saying no file holds a word ('Without X: N files', 'No file holds …'), run at most ONE more search (dropping that word); then report from what you have.",
            "Search again only to follow a lead the results give (a spelling they show, a folder), not to re-check what was found.",
            "'All', 'every', 'everything about' is still a find: answer from the cards; do not open files or compute totals unless the user asked for a total or a value.",
            "A value question opens the file (when the card is under 90%) even if the card looks unrelated to the user; a find question never opens it.",
        ]

        reading_the_results: [
            "Drive results say how each file matched. 'About' and 'mentioned in' are exact: the file's own name, details, place or text holds the words, in their spellings and word forms. 'By translation' and 'labelled' are the index's automatic words — passage keywords with their source quote, type, topic and life-area words — and may be wrong or incomplete. 'Related by meaning only' lines are guesses. Other spellings and word forms were searched; other languages only where the index wrote them. The coverage line says what the search could not see. When a word is held by no file, the result counts the files that match the other words, so you can tell whether the word was phrasing or the subject.",
            "Trust the exact tiers. When the answer depends on an automatic one, check its quoted source or open the file.",
            "Mention 'related by meaning only' lines only when they help, never as findings.",
            "'Coverage: complete' makes a not-found reliable.",
        ]

        card_accuracy: [
            "A described file's result carries 'card accuracy: N%'. 100% — everything in this card is confirmed by the file's own text; you can trust the card fully. The lower, the more likely the card's values contain errors.",
            "'Find', 'which files', 'do I have': answer from the cards at any accuracy. Never open files for this.",
            "A question about a value in a file — a number, a date, an amount, an expiry: answer from the card when its accuracy is 90% or more. Below that, open the file and check the value before answering.",
            "A result saying the bot cannot open the file: the file exists. Do not try to open it. This line overrides the stale and accuracy rules: answer from the card and say it may be out of date; do not open the file.",
        ]

        when_nothing_is_found: [
            "When a folder card is shown under About (its name holds the words), search within it (folder) or list it before answering that nothing was found.",
        ]

        answering_from_documents: [
            "Numbers: search passages are windows, not whole tables. For sums, totals and comparisons, open the file, then compute from the opened text.",
            "Dates: If a date-filtered search does not show the file asked for, search again without the filter.",
            "Freshness: results show the drive as of the time they state. If the user refers to a recent edit, or a result is marked stale, open the file (it is read live) before quoting it.",
        ]

        recordings: [
            "A recording is found by what was said; open its ref (open_drive_file) for the transcript; use get_recording_link only when the user wants the recording itself, and it comes as a link.",
            "A recording may be audio or video (.webm is video). Dates in file names appear as YYYY-MM-DD: search the date in that form too. When a search does not find a recording, listing its media folder is a valid fallback.",
            "The transcript is also a file in the bot's _Transcripts folder on the drive. That folder is managed by the bot: read it, never write to it (a save, edit, move or delete there is refused). A transcript of a recording that was deleted stays in its Archive; it is never found by a default search.",
        ]

        deleted_files: "Search deleted files (search_deleted_recordings_and_notes) only when the user asks about files that were deleted or removed; it finds transcripts of deleted recordings and your notes on deleted files. An empty query lists the newest. Open a hit with its drive: ref to read the transcript. It is never for an ordinary search."

        changes: [
            "copy_file_to_drive and save_text_file save to Inbox unless the user named a folder; give folder only when the user named one.",
            "The drive never overwrites: the result states the name actually used.",
            "save_text_file writes one file_contents item: the name ends in .md .txt .csv .json .yaml .yml .html .svg or .xml; up to 5 MiB of text. The text comes only from file_contents. When the commission asks for a text to be written and drive_state lists no item, do not write it yourself: call the tool with text_item t1; the system refuses and tells the orchestrator how to attach the text.",
            "A chat file is copied (copy_file_to_drive), never moved; a drive: ref is never a copy source.",
            "append_to_file adds text only to text files. Rewriting a file with new text is not available (report it as not_done); replace_file_content replaces a file's whole content with another file.",
            "Move all X from A to B: list A, then one move_file per matching item, in one turn.",
            "Delete removes a file or a whole folder; it is recoverable from the drive's recycle bin, and the user gets a notice.",
        ]
    }
