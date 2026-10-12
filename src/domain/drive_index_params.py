"""
Drive indexing parameters (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §15). Initial values; S1, S3 and the
eval may revise them. Slice 1 subset — content parameters arrive with Slice 2.
"""
from datetime import timedelta

TICK_INTERVAL = timedelta(minutes=10)
TICK_DEADLINE_S = 1800                      # Cloud Tasks HTTP maximum = indexer service timeout
TICK_START_BUDGET = timedelta(minutes=8)    # no new work starts after it (§7.1 invariant)
TICK_FILE_BUDGET = 50                       # index runs per tick (Slice 2)
MAINTENANCE_SHARE = 0.30                    # classes 3-4 share of the tick budget (§7.8)
MAX_ATTEMPTS = 3
RETRY_BASE = timedelta(minutes=5)
RETRY_CAP = timedelta(hours=6)
EMBED_IN_FLIGHT = 4
MAX_CARD_RECORDS_PER_USER = 25_000
K = 8
CARD_FETCH = 50
NAME_FETCH = 100
FETCH_MAX = 1_000                           # Firestore find_nearest limit; the widened fetch (§10.1 step 5)
DENSE_FOLDER_CHILDREN = 500
PROBE_CHILDREN = 200
ITEM_CALLS_PER_TICK = 20                    # RFC Δ9: probes, parent lookups, re-listing pages
RELIST_PAGE = 200                           # children per re-listing page (one item call)
FOLDER_INMEM_CARDS = 1_000
CASCADE_OPS_PER_TICK = 2_000
NAME_MIN_TRIGRAMS = 3
NAME_QUERY_KEYS_MAX = 30                    # Firestore array-contains-any limit
NAME_KEY_QUERIES = 8                        # whole-token keys, and trigram keys, each queried one by one (at most
                                            # this many of each) before one query with the rest (RFC Δ17)
STALE_ALERT_AFTER = timedelta(hours=2)
ACCESS_LOSS_TTL = timedelta(days=30)
LEASE_MARGIN = timedelta(minutes=1)         # lease_until = start + TICK_DEADLINE + margin (§7.1)
NUDGE_RETRY_DELAY_S = 60                    # a live lease ends within TICK_DEADLINE: at most ~31 retries
PLACE_MAX_DISTANCE_KM = 50.0                # RFC Δ7
PLACE_PREFER_RADIUS_KM = 15.0               # a heavier place within it may name a photo (RFC Δ39)
RRF_K = 60
SYSTEMIC_BACKOFF_BASE_S = 60                # continuation delay after a systemic error, doubled per failure
SYSTEMIC_BACKOFF_CAP_S = 1800               # = 3 x TICK_INTERVAL
RECORD_WRITE_BATCH = 400

# -- content (Slice 2+4, §15, §24.13) ------------------------------------------------------------------------------
QUIET = timedelta(minutes=10)               # S0b: Word for the web saves every 1.2-3.3 min while typing (§7.3)
MAX_DEFER = timedelta(minutes=60)
DOWNLOAD_TIMEOUT_S = 120
EXTRACT_WALL_TIMEOUT_S = 120                # S1 may revise
EMBED_FILE_BUDGET_S = 180
MODEL_FILE_BUDGET_S = 600                   # §24.3: windows done by then are indexed, the rest `truncated`
FILE_START_MARGIN_S = 60
MAX_DOWNLOAD_BYTES = 45 * 1024 * 1024       # Step 1 drive cap
MAX_SHEET_DOWNLOAD_BYTES = 10 * 1024 * 1024 # XLSX/XLS (Phase 0 amplification)
MAX_TEXT_CHARS = 2_000_000
CHUNK_CHARS = 2_400
CHUNK_OVERLAP = 240
MAX_CHUNKS_PER_FILE = 400
MAX_CHUNKS_PER_USER = 50_000
MAX_SHEET_ROWS = 5_000
CARD_LEAD_CHARS = 2_000
CHUNK_FETCH = 300
FOLDER_INMEM_CHUNKS = 2_000
CONTENT_TERM_QUERIES = 8                    # one array-contains query per query term, at most this many
CONTENT_TERM_FETCH = 100
MODEL_EXTRACT_DENSITY = 200                 # chars per page below which a PDF is routed to the reader (§9)
MODEL_EXTRACT_GARBAGE = 0.05                # share of unreadable characters above which it is routed
MODEL_EXTRACT_MAX_PAGES = 200
MODEL_EXTRACT_PAGES_PER_MONTH = 5_000       # ≈ $3 per user per month on Haiku 5.5 (§24.9)
MODEL_PAGES_PER_CALL = 10                   # far below the 100k-token price step (§24.3)
MODEL_IN_FLIGHT = 4
TRIAGE_BATCH = 16
TRIAGE_IMAGES_PER_MONTH = 20_000
TRIAGE_THUMBNAIL_SIZE = "medium"            # Graph 176 px; "large" (800 px) is the live-check alternative
READER_RENDITION_SIZE = "c2048x2048"        # Graph custom size for the reader's image input (S0g)
MODEL_BACKOFF_BASE_S = 60                   # model outage backoff, doubled per consecutive outage (§24.8)
MODEL_BACKOFF_CAP_S = 3600
