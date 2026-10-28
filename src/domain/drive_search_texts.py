"""
Drive search texts — the verbatim strings of Search v2's result, agent manifest and Smart protocol
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §25.5.6, §25.5.7, frozen in §25.13.6). Pure constants.

Defined by the frozen core and not wired yet: P6 renders with them (`render_results`), puts the manifest texts into
`agent_manifest` and the tiers line into the protocol token. `REFERENCE_REMINDER` and the "index reflects the drive as
of" line stay in `drive_search` unchanged. Templates use `str.format` fields; a word is passed already quoted where
the template does not quote it.

What is frozen (review MINOR 12, stated in §25.13.6): the strings of §25.13.6's example, its D29, footer and migration
lines, the manifest field texts and the protocol tiers line. The other coverage parts (not read yet, could not be
read, building, re-keying, a full page, keys cut, concepts cut, no doc2query), the other match sources (photo location,
topic, life area, validity, photo description, entity, value, date, kind), the card's `index:` line, the intent's
capability description and plural forms ("1 file", "1 text file is") are P6's, inside these templates' style.

Public interface:
    render: SEARCHED_LINE, SEARCHED_IGNORED, SEARCHED_COMMON, COVERAGE_COMPLETE, COVERAGE_INCOMPLETE,
            COVERAGE_NOT_DESCRIBED, COVERAGE_NOT_DESCRIBABLE, the tier headings, SHOWING, SHOWING_AT_LEAST,
            the card lines, GROUP_LINE, RELATED_SMALL_NOTE
    D29:    ABSENT_LINE, ABSENT_IN_SCOPE_LINE, WITHOUT_LINE, WITHOUT_AT_LEAST_LINE; with incomplete coverage
            SEARCHED_ABSENT_LINE, SEARCHED_ABSENT_IN_SCOPE_LINE (xc F4)
    footers: FOOTER_EXACT, FOOTER_NO_CONJUNCTION, CONJUNCTION_TERM; FOOTER_EXACT_SEARCHED,
             FOOTER_NO_CONJUNCTION_SEARCHED (xc F4)
    card:   VALID_UNTIL_LINE, DATE_EXPIRES (xc F5), CAPTURE_LINE (§26.3), SCENE_LINE (D40), CARD_ACCURACY_LINE (§26.4)
    MIGRATION_LINE (superseded), REBUILDING_LINE (D30, xc XC-1)
    access (§26.2, D33): ACCESS_LINE_FILE, ACCESS_LINE_FOLDER (the card's `index:` line of an item the bot cannot
            open), ACCESS_NOTE_FILE, ACCESS_NOTE_FOLDER (list and open, Δ55), COVERAGE_INACCESSIBLE (Δ56),
            ENVELOPE_INACCESSIBLE (a filter listing), STALE_INACCESSIBLE (Δ52)
    manifest: DRIVE_QUERY_DESCRIPTION, DRIVE_KIND_DESCRIPTION, DRIVE_DATE_FROM_DESCRIPTION, DRIVE_DATE_TO_DESCRIPTION,
              DRIVE_DELETED_DESCRIPTION (shared with the owner-notes RFC §6.4)
    PROTOCOL_TIERS_LINE
"""

# -- render (§25.13.6) ---------------------------------------------------------------------------------------------
SEARCHED_LINE = ("Searched names (several spellings and word forms), file descriptions, document text and passage "
                 "keywords for: {concepts}")                                  # then the optional parts, then "."
SEARCHED_IGNORED = "; ignored: {words}"
SEARCHED_COMMON = "; ignored as common: {words}"                              # §25.5.4

COVERAGE_COMPLETE = "Coverage: complete."
COVERAGE_INCOMPLETE = "Coverage: incomplete — {parts}."                       # parts joined by "; "
# Books, sheets and data files get no passage keywords by policy (§25.3): not a coverage gap, a note on an empty result
NO_PASSAGES_NOTE = "{files} own words only, with no translated or synonym keywords."
COVERAGE_NOT_DESCRIBED = "not described yet"                                  # ready, deferred, parked
COVERAGE_NOT_DESCRIBABLE = "could not be described"                           # held

ABOUT_HEADING = 'About "{query}" ({count}):'
MENTIONED_HEADING = "Mentioned in ({count}):"
TRANSLATED_HEADING = "By translation (automatic) ({count}):"
LABELLED_HEADING = "Labelled (automatic) ({count}):"
RELATED_HEADING = "Related by meaning only, unverified ({count}):"
SHOWING = "showing {shown} of {total}"                                         # replaces {count} past the shown cap
SHOWING_AT_LEAST = "showing {shown} of at least {total}"                      # a page was full

MATCHED_LINE = "matched: {sources}"                                            # sources joined by "; "
MATCH_NAME = 'name "{word}"'
MATCH_PLACE_IN_FILE = 'place "{word}" (named in the file)'
MATCH_WORDS_IN_PASSAGE = "words {words} in one passage"
MATCH_PASSAGE_KEYWORD = 'passage keyword "{word}", from "{evidence}"'
MATCH_TYPE_LABEL = 'type label "{word}" (automatic)'
NOT_MATCHED_COMMON = "not matched (common)"
KEYS_CUT = "keys cut"
SIMILAR_NAME = "similar name"
DATE_LINE = "date: {iso} ({source})"                                           # source: drive_search.DATE_*
DATE_EXPIRES = "expires"                     # the date line's source when the match came through validity words (F5)
VALID_UNTIL_LINE = "valid until: {iso}"      # every card of a file with a validity or due date (xc F5)
SCENE_LINE = "scene: {labels}"   # a photograph's scene labels (D40), in their English names
CAPTURE_LINE = "capture: {capture} ({source})"   # every card of a file with a capture (§26.3): source detected/automatic
CARD_ACCURACY_LINE = "card accuracy: {percent}%"   # a card with a score (§26.4): never in the card text, so no re-embed
PASSAGE_LINE = "passage: … {text} …"
GROUP_LINE = "{folder}: {count} more {tier} files ({kinds}, {dates})"
RELATED_SMALL_NOTE = "closest by meaning; the drive is too small to tell related from unrelated"

# -- D29: an absent concept ----------------------------------------------------------------------------------------
ABSENT_LINE = 'No file holds "{word}".'
ABSENT_IN_SCOPE_LINE = 'No file in this scope holds "{word}".'               # the word exists outside the filter/folder
WITHOUT_LINE = 'Without "{word}": {count} files match the other words.'
WITHOUT_AT_LEAST_LINE = 'Without "{word}": at least {count} files match the other words.'
# The same statements when the coverage line names a part the search could not see (xc F4): an absence is stated only
# over what was searched (§25.5.4 "only when the scope was read")
SEARCHED_ABSENT_LINE = 'No searched file holds "{word}".'
SEARCHED_ABSENT_IN_SCOPE_LINE = 'No searched file in this scope holds "{word}".'

# -- footers -------------------------------------------------------------------------------------------------------
FOOTER_EXACT = "No other file is about or mentions all of these words."
FOOTER_NO_CONJUNCTION = "No file matches all of: {terms}."                    # terms: CONJUNCTION_TERM joined by ", "
CONJUNCTION_TERM = '"{word}" ({count} files)'
FOOTER_EXACT_SEARCHED = "No other searched file is about or mentions all of these words."      # coverage incomplete
FOOTER_NO_CONJUNCTION_SEARCHED = "No searched file matches all of: {terms}."                   # coverage incomplete

# A sync state not rebuilt under v2 (D30, xc XC-1): no stream runs, nothing is stated absent
REBUILDING_LINE = ("The index is being rebuilt for a new search format, so nothing could be searched yet: this "
                   "result says nothing about what the drive holds. Browse the folders, or search again later.")

# Superseded by D30 (P6 review M3): v2 has no migration mode and never renders this line; a state not rebuilt under v2
# is not searched and answers REBUILDING_LINE (XC-1). Kept only because the frozen core pins it; drop it with that pin.
MIGRATION_LINE = ("The index is being migrated to a new search format: only names, meaning and filters were "
                  "searched.")

# -- Smart protocol, `drive_specialist` block (§25.5.7) ------------------------------------------------------------
PROTOCOL_TIERS_LINE = (
    "Drive results say how each file matched. 'About' and 'mentioned in' are exact: the file's own name, details, "
    "place or text holds the words, in their spellings and word forms. 'By translation' and 'labelled' are the "
    "index's automatic words — passage keywords with their source quote, type, topic and life-area words — and may "
    "be wrong or incomplete. 'Related by meaning only' lines are guesses. Other spellings and word forms were "
    "searched; other languages only where the index wrote them. The coverage line says what the search could not see. When a word is held by no file, the result counts "
    "the files that match the other words, so you can tell whether the word was phrasing or the subject."
)


# -- files the bot cannot open (§26.2, D33) --------------------------------------------------------------------------
ACCESS_LINE_FILE = ("index: the file exists, but the bot cannot open it (its sharing settings keep the bot out); to fix "
                    "it, copy the file into a new folder you create inside the bot's folder: the copy can be read.")
ACCESS_LINE_FOLDER = ("index: the folder exists, but the bot cannot open this folder or its files (their sharing "
                      "settings keep the bot out); to fix it, copy the files into a new folder you create inside the "
                      "bot's folder: the copies can be read.")
# list and open answer from the index on a "not found" (Δ55): the same sentence, outside a card
ACCESS_NOTE_FILE = "The" + ACCESS_LINE_FILE[len("index: the"):]
ACCESS_NOTE_FOLDER = "The" + ACCESS_LINE_FOLDER[len("index: the"):]
COVERAGE_INACCESSIBLE = "cannot be opened by the bot"            # "{n} files cannot be opened by the bot" (Δ56)
ENVELOPE_INACCESSIBLE = ("{count} cannot be opened by the bot (their sharing settings keep it out), so their content "
                         "was not searched.")
STALE_INACCESSIBLE = "stale: the indexed text is from before the bot lost access to the file, and may be out of date"
