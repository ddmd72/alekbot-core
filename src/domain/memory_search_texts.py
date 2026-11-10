"""Texts of the memory search that more than one caller reads: the agent's manifest entry and the connector tool
(fact profile RFC §5.4). One text, no copies."""

QUERIES_FIELD_TEXT = (
    "The search phrases, in English, one to four of them. A record is one statement, so a phrase finds it best when the "
    "phrase reads like such a statement: it answers at most once each of who, what, where, when and why, and leaves out "
    "what it does not know. A phrase holding two people, two actions, two places or two times sits between the records "
    "about each and close to none of them; split it into one phrase per subject instead. When the user's need touches "
    "several subjects, or one subject from several sides (what the matter is and what was done about it), send one phrase "
    "for each. List the phrases in order of priority: first the one the answer depends on most, then the rest in falling "
    "importance. The results are merged by that order, so the first phrase's records come first and a later phrase adds "
    "what the earlier ones missed. The search never sees your conversation: write the person, place or time instead of "
    "\"he\", \"there\" or \"last time\", and put in only what the conversation tells you, since a guessed detail moves the "
    "phrase toward the wrong records."
)

SEARCH_MEMORY_DESCRIPTION = (
    "The user's own memory: the facts of their life, the people around them, places, plans, preferences, past events and "
    "decisions. Each stored record is a short English statement about one matter of the user's life, found by meaning "
    "from the phrases sent in context.queries. An empty result means nothing is stored on that matter; it is an answer, "
    "not an error."
)

NO_QUERIES_NOTE = (
    "Tool error: search_memory reads its phrases from context.queries — one to four English phrases, most important "
    "first — and none were given."
)

NO_RECORDS_TEXT = "No records matched."
