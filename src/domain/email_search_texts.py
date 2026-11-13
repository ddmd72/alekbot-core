"""
Email search texts — the code-written lines of a mail search result (email index v2 §5.4, §2.5; search slice 4).
Pure constants, in English, as the drive's (`drive_search_texts`): the model reads them, the owner never sees them
unless Smart quotes them.

A line that says what the index does not hold (`NOTHING_LINE`, `ABSENT_LINE`) is a statement about the carded mail
only: the recard line says how much mail is not searched yet. A count of 1 has its own singular line.
"""

HEAD_RANKED = ("Indexed mail, best match first. Each card was written from its mail when it was indexed; on a matched: "
               "line, (printed) means the mail prints it as shown, (by translation) another spelling of it, "
               "(automatic) a word the indexer wrote. A file shown as not read (a scan or a locked file) was not read: "
               "its content is unknown. Open the mail before quoting a detail that is not on its card.")
HEAD_MEANING = "Closest by meaning only (no word of the request matched these cards):"
NOTHING_LINE = "No indexed mail matches: for the index this answer is final."
ABSENT_LINE = 'No indexed mail names "{word}".'
WITHOUT_ONE = "Without it: {count} mails match the rest."            # one absent word
WITHOUT_ONE_SINGULAR = "Without it: 1 mail matches the rest."
WITHOUT_MANY = "Without these words: {count} mails match the rest."  # several absent words
WITHOUT_MANY_SINGULAR = "Without these words: 1 mail matches the rest."
FOOTER_EXACT = "No other indexed mail matches all of these."
SHOWING_LINE = "Showing {shown} of {total} matching mails."
FOLD_LINE = "  + {count} more of the same kind from {domain}, {span}"     # span: "Jan 2025 – Oct 2026" | "Mar 2026"
LIST_ALL_LINE = "{count} indexed mails for {period}, all shown, newest first."
LIST_ALL_LINE_SINGULAR = "1 indexed mail for {period}, shown."
LIST_FIRST_LINE = "{count} indexed mails for {period}; the newest {shown} are shown."
LIST_NONE_LINE = "No indexed mail for {period}."
RECARD_LINE = "Re-carding in progress: {count} indexed mails still have old entries and are not searched here."
RECARD_LINE_SINGULAR = "Re-carding in progress: 1 indexed mail still has old entries and is not searched here."
