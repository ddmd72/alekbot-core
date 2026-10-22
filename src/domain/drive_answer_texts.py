"""Every new string of the drive specialist's answer to the orchestrator (DRIVE_SPECIALIST_RFC §6, §7.3, §7.5).

The reading lines are the former `drive-search` system skill's orchestrator-side rules, verbatim from its SKILL.md
(deleted at the cut, DRIVE_SPECIALIST_RFC §10 step 4; markdown emphasis dropped; a field name the orchestrator no
longer has becomes plain words, §7.6), plus the one line A′ adds for opened files. Everything else the answer carries
is today's text.
"""
from __future__ import annotations

from .drive_search_texts import PROTOCOL_TIERS_LINE

# -- the answer shape (§7.1, §7.2) -------------------------------------------------------------------------------
STATUS_HEAD = "Drive result: {status}"
WHAT_TO_DO = "What to do: {line}"
DONE_LINE = "Done."
ANSWER_FROM_RESULTS = "Answer from the results below."
ONLY_PART = "Only part was done:"
PARTIAL_NOT_DONE = "Not done: {not_done}"
UNFINISHED_LINE = ("The drive assistant could not finish; what ran is listed below. Tell the user what ran and what "
                   "did not; do not commission the same request again in this turn.")
NOTHING_DONE = "Nothing was done: {why}"
NOTHING_RAN = "no drive operation ran"
NOT_ATTACHED = ("Nothing was written: the text was not attached. Commission again with context.file_contents holding "
                "the full text.")
OP_LINE = "{step}. {verdict}: {text}"
OP_DONE = "done"
OP_NOT_DONE = "not done"
READING_PREFIX = "Reading: "
NOTE_PREFIX = "Specialist note: "
NOTE_MAX_CHARS = 300
RESULT_HEAD = "[Result {handle}, step {step}: {kind}]"
RESULT_HEAD_BEFORE = "[Result {handle}, step {step}: {kind}, before the changes in {steps}]"

# -- the result kinds in the headers -------------------------------------------------------------------------------
KIND_SEARCH = "search"
KIND_DELETED_SEARCH = "deleted recordings and notes search"
KIND_LISTING = "listing"
KIND_OPEN = "open"
KIND_LINK = "recording link"

# -- what the specialist's own model sees instead of an opened body (§6) -------------------------------------------
STAND_IN_TEXT = ("[{handle}: opened {label}, {chars} characters, delivered to the orchestrator as is; you do not see "
                 "the text]")
STAND_IN_FILE = "[{handle}: {label} ({mime}) attached for the orchestrator]"
SAME_SEARCH = "[same as {handle}: this exact search already ran in this request; its result is above. Not run again.]"
DID_NOT_FINISH = "did not finish in time"
NOT_STARTED = "not started: the time for this request ran out"
NO_ITEM = ("No file_contents item '{value}' (items: {known}). Nothing was written: the text must come from the "
           "orchestrator in file_contents.")
NO_ITEMS = "none"
FIND_REF = "find it with search_drive or list_folder"
REF_REQUIRED = "{field} is required: {what}."   # an empty ref is refused before any call (CRUD: empty is not given)

# -- reading lines (§7.3), verbatim from the drive-search skill ----------------------------------------------------
R_FIND = ('"Find", "show me", "which files", "everything about X", "do I have …" — the user wants the files. Answer '
          "from the search results alone: each result already says what the file is, where it is, its date and why it "
          "matched. Do not open files to confirm them.")
R_TIERS = PROTOCOL_TIERS_LINE
R_TRUST = "Trust the exact tiers. When the answer depends on an automatic one, check its quoted source or open the file."
R_RELATED = 'Mention "related by meaning only" lines only when they help, never as findings.'
R_COVERAGE = '"Coverage: complete" makes a not-found reliable.'
R_MANY = ("Many results: give the count, group them (by folder, type or date) and name the few most relevant; offer to "
          "narrow down. Never open them one by one to describe them.")
R_ALL = ('"All", "every", "everything about" is still a find: answer from the cards; do not open files or compute '
         "totals unless the user asked for a total or a value.")
R_PASSAGE = ("A passage is a window cut from the file around what matched. Before quoting more than it shows, open the "
             "file. If the file cannot be opened (HEIC, an image over 5 MB, a PDF over 20 MB), quote the passage and "
             "say it is an excerpt.")
R_ACCURACY = ("A described file's result carries `card accuracy: N%`. 100% — everything in this card is confirmed by "
              "the file's own text; you can trust the card fully. The lower, the more likely the card's values contain "
              "errors.")
R_ACCURACY_FIND = '"Find", "which files", "do I have": answer from the cards at any accuracy. Never open files for this.'
R_ACCURACY_VALUE = ("A question about a value in a file — a number, a date, an amount, an expiry: answer from the card "
                    "when its accuracy is 90% or more. Below that, open the file and check the value before answering.")
R_SCAN = ("For a scan or a photo of a document, the file's text is a reading of the image; when a value matters to "
          "the user, say where you read it.")
R_NO_ACCESS = ("A result saying the bot cannot open the file: the file exists. Tell the user to copy each file into a "
               "new folder they create inside the bot's folder (a copy in the same folder can stay closed); the copy "
               "can be read. Do not try to open it. This line overrides the stale and accuracy rules: answer from the "
               "card and say it may be out of date; do not open the file.")
R_PHOTOS = "Photos: answer from the description, place and date in the result."
R_VERSIONS = ("Versions: when several results look like versions of one document and their values differ, never pick "
              "silently: answer from the version its name or date marks as latest, say which, and list the others "
              "with their dates.")
R_BUILDING = ("While the index is still being built or rebuilt, never say a document does not exist: say the index is "
              "not complete yet and offer to list the folder.")
R_NOT_SEARCHED = ("When the coverage line names what the search could not see (documents not read yet or not described "
                  "yet, a truncated card, only names and metadata searched), a not-found says what was not searched "
                  "(e.g. '3 documents are not read yet') and offers to list or open the folder.")
R_FOLDER_CARD = ("When a folder card is shown under About (its name holds the words), search within it or list it "
                 "before answering that nothing was found.")
R_NUMBERS = ("Numbers: search passages are windows, not whole tables. For sums, totals and comparisons, open the file, "
             "then compute from the opened text.")
R_DOC_VS_MEMORY = ("Document vs memory: a document is authoritative for its own contents (its terms, amounts, dates). "
                   "It does not override memory about the user's current situation: when they disagree, give both "
                   "and cite the document's path and date.")
R_NOT_READ = ("The drive assistant did not read this file; if it points to another file or a term to look for, "
              "commission again with it.")
