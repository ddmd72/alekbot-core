"""
Email card params — the numbers of the email card (email index v2, card slice 1). Pure constants.

The answer caps are the lean card's (measured 2026-11-13: forms 3, word lists 12, dates 5), each at or under the drive's
own cap (`drive_index_params`), which the reused grading and keyer still apply: a mail cap above the drive's would be
cut silently, and raising a drive constant re-keys every drive. People is the mail's own field.
"""
from .drive_index_params import (
    ENTITY_FORMS_MAX, PROFILE_DATES_MAX, PROFILE_ENTITIES_MAX, PROFILE_KEY_VALUES_MAX, PROFILE_TOPICS_MAX,
    PROFILE_WORD_LIST_MAX,
)

CARD_VERSION = 1                       # a bump re-cards stored mail (and re-buys the reading of scanned documents)

# the model's answer, cut by the parser (the writer is told these numbers)
MAIL_ENTITIES_MAX = PROFILE_ENTITIES_MAX           # 12
MAIL_ENTITY_FORMS_MAX = min(3, ENTITY_FORMS_MAX)   # the lean card: another script or language only
MAIL_KEY_VALUES_MAX = PROFILE_KEY_VALUES_MAX       # 12
MAIL_DATES_MAX = PROFILE_DATES_MAX                 # 5: a range is one date
MAIL_TOPICS_MAX = PROFILE_TOPICS_MAX               # 8, and as many native topics
MAIL_WORD_LIST_MAX = min(12, PROFILE_WORD_LIST_MAX)   # topic words in English and the mail's language
MAIL_PEOPLE_MAX = 6
MAIL_LIFE_DOMAINS_MAX = 3
MAIL_TITLE_CHARS = 120
MAIL_WHAT_IT_IS_CHARS = 300

# the extraction input (§3): body, quoted history, attachments, the whole
BODY_HEAD_CHARS = 8_000
BODY_TAIL_CHARS = 2_000
QUOTE_KEEP_CHARS = 500                 # the head of a quoted earlier mail kept in a reply
ATTACHMENT_HEAD_CHARS = 6_000
ATTACHMENT_TAIL_CHARS = 1_500
ATTACHMENTS_READ_MAX = 3               # read attachment texts per mail
ATTACHMENT_MIN_CHARS = 1_500           # an included attachment text is never cut below this to fit the whole
INPUT_CHARS_MAX = 24_000               # the mail block's target size; the attachments' share is cut to fit

# what Smart sees per result (§2.5)
RESULT_LINE_CHARS_MAX = 350
