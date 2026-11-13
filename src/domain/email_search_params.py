"""
Email search params — the numbers and word lists of email search v2 (email index v2 §5, search slice 4). Pure
constants.

The caps are lines, not mails: a folded group is one line. `MAIL_GENERIC_WORDS` are the words a mail request carries
that no card holds as a topic (the mail nouns and the arrival verbs: "what did Example School send in March"), removed
from the request before its concepts are made, in the product languages (en, es, fr, uk, ru); the drive keeps its own
Russian forms in its tables the same way. Never `message` / `messages`: a mail-only doc type's name.
"""
from typing import FrozenSet

from .drive_index_params import MEANING_POOL

MEANING_TOP = MEANING_POOL        # 50, the card kNN page
KEY_PAGE = 200                    # cards per key-stream fetch; a full page makes a document frequency a lower bound
SHOWN_FIRST = 10                  # rank group 0 (About, or an exact-key hit)
SHOWN_REST = 8                    # rank groups 1-3 together (By translation, Labelled, found only by a slot list)
SHOWN_MEANING = 5                 # rank group 4 (meaning only)
SCOPE_ALL_LINES = 50              # `scope=all`: groups 0-3 together
LIST_LINES_MAX = 50               # list mode lines (a folded group is one line)
LIST_PAGE = 500                   # cards per month-key fetch in list mode
UPCOMING_DAYS = 365               # an `upcoming` window with no end
EXACT_KEYS_MAX = 3
PEOPLE_SLOTS_MAX = 6
SUBJECT_CHARS = 200
MONTHS_PER_FETCH = 30             # month keys per `list_in_months` fetch (the port's array-contains-any limit)

MAIL_GENERIC_WORDS: FrozenSet[str] = frozenset("""
email emails e-mail e-mails mail mails inbox mailbox send sends sent received receive came come arrived
correo correos buzón enviado enviada enviaron envió recibido recibida recibí llegó llegaron
courriel courriels boîte envoyé envoyée reçu reçue reçus
лист листи листів листа листом пошта пошти пошту надіслав надіслала надіслали прийшов прийшла прийшло прийшли отримав
письмо письма писем почта почты почту прислал прислала прислали пришло пришли пришел получил
""".split())
