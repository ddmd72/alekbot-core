"""
Drive indexing parameters (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §15, §24.13). Initial values; S1, S3 and the
eval may revise them. Slice 1 parameters first, then the content parameters of Slice 2+4.
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
WRAPPER_TAG_GAP_MAX = 16                    # spaces / invisible characters a wrapper-tag look-alike may hold
                                            # between its parts: bounded so the escape stays linear on hostile
                                            # whitespace (final review F3-3); the per-result nonce is the defence

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
WINDOW_CHARS_TOTAL = 12_000                 # all content windows of one result set (≈ 3-4k tokens): best chunks
                                            # first, neighbours in rank order while it lasts (§10.2)
QUERY_TERM_CHARS = 1000                     # the search extracts query terms from at most this many characters of
                                            # drive_query, cut at a word boundary (no length cap upstream; arbiter R2 S-m1)
CONTENT_TERM_QUERIES = 8                    # one array-contains query per query term, at most this many
CONTENT_TERM_FETCH = 100
TERM_NONSELECTIVE_FILES = 8                 # a query term whose FULL first term page spans more distinct files than
                                            # this is non-selective: the page is an id-ordered slice of many files,
                                            # so it is dropped from fusion, anchoring, trimming and widening. A full
                                            # page from few files (an IBAN in every chunk of one contract) still
                                            # selects (§24.6, arbiter R1-2)
MODEL_EXTRACT_DENSITY = 200                 # chars per page below which a PDF is routed to the reader (§9)
MODEL_EXTRACT_GARBAGE = 0.05                # share of unreadable characters above which it is routed
MODEL_EXTRACT_MAX_PAGES = 200
MODEL_EXTRACT_PAGES_PER_MONTH = 5_000       # ≈ $3 per user per month on Haiku 5.5 (§24.9)
MODEL_PAGES_PER_CALL = 10                   # far below the 100k-token price step (§24.3)
MODEL_IN_FLIGHT = 4
TRIAGE_BATCH = 16
TRIAGE_IMAGES_PER_MONTH = 20_000
TRIAGE_THUMBNAIL_SIZE = "large"             # Graph 800 px, chosen after the live check (2026-10-13): captions more
                                            # precise than at 176 px ("medium"). TRIAGE_VERSION is not bumped: images
                                            # already triaged keep their verdict until the owner rebuilds the index
READER_RENDITION_SIZE = "c2048x2048"        # Graph custom size for the reader's image input (S0g)
MODEL_BACKOFF_BASE_S = 60                   # model outage backoff, doubled per consecutive outage (§24.8)
MODEL_BACKOFF_CAP_S = 3600
MODEL_REREAD_COOLDOWN = timedelta(hours=24)  # a file the reader read is not read by it again sooner: the edits made
                                            # meanwhile wait, the newest version is read once it ends (final review
                                            # F1-3: a scan saved every few minutes was re-read, and re-billed, hourly)

# -- Search v2 (§25.12; defined by the frozen core, read from P1-P7 on) --------------------------------------------
# Two names clash with a v1 value still in use: the v2 value carries a _V2 suffix until its part replaces the v1 one
# (TRIAGE_BATCH: P3 triage v2; WINDOW_CHARS_TOTAL: P6 render). Retired v1 names (§25.12) stay until their readers go.
PROFILE_MIN_CHARS = 200                     # under it a note gets a template profile and no chunk/hint score (§25.3)
PROFILE_REFRESH_COOLDOWN = timedelta(hours=24)
PASSAGE_REFRESH_COOLDOWN = timedelta(hours=24)
PASSAGE_CHUNKS_PER_CALL = 2
PASSAGE_MAX_CHUNKS_PER_FILE = 100           # ≈ 240 000 chars of a file get passage hints (D17 acknowledgement)
PASSAGE_REUSE_SHARE = 0.8                   # share of a new chunk's paragraphs from one old chunk to reuse its hints
PASSAGE_MIN_SIMILARITY = 0.5                # calibrated (§25.7)
TRIAGE_BATCH_V2 = 8                         # replaces TRIAGE_BATCH (§25.4.3)
PROFILER_OUTPUT_TOKENS = 4_096              # output caps: schema worst case + reasoning allowance (§25.4.4)
PASSAGES_OUTPUT_TOKENS = 4_096              # for PASSAGE_CHUNKS_PER_CALL chunks
TRIAGE_OUTPUT_TOKENS = 8_192                # for TRIAGE_BATCH_V2 photos
READER_USD_PER_MONTH = 4.0                  # per user per calendar month, at the model that ran (§25.4.5)
TRIAGE_USD_PER_MONTH = 3.0
PROFILE_USD_PER_MONTH = 4.0
PASSAGE_USD_PER_MONTH = 20.0
INITIAL_BUILD_USD = 40.0                    # once per user (D27; owner minimum $20)
FALLBACK_ALERT_SHARE = 0.5                  # of a user's model cost in a day
ENRICH_SHARE = 0.40                         # at least this share of the tick start budget goes to enrichment
ENRICH_IN_FLIGHT = 3                        # files enriched at once
PASSAGE_IN_FLIGHT = 2                       # passage calls per file at once, all under MODEL_IN_FLIGHT
ENRICH_FILE_BUDGET_S = 300                  # hard wait_for around one file's enrichment (§25.4.5)
WORD_KEYS_PER_CHUNK = 900                   # key caps (§25.2.6)
HINT_KEYS_PER_CHUNK = 60
ABOUT_KEYS_PER_RECORD = 600
WRITE_BATCH_BYTES = 6_000_000               # write batches by bytes too, besides RECORD_WRITE_BATCH (Firestore 10 MiB)
README_SCAN_CHILDREN = 1_000                # children a collapsed folder's README step lists at most (§25.3)
MAX_CONCEPTS = 8                            # query concepts (§25.5.1)
CONCEPT_KEYS_MAX = 30                       # keys per emitted list (Firestore array-contains-any)
ABOUT_FETCH = 500                           # candidate streams (§25.5.2)
WORD_FETCH = 300
HINT_FETCH = 300
WORD_FILES_MAX = 60
MEANING_POOL = 50                           # meaning score (§25.5.3)
MEANING_POOL_MIN = 12
MIN_SPREAD = 0.01
Z_RELATED = 2.5                             # then per drive-size band, calibrated (§25.7)
DRIVE_SIZE_BANDS = (200, 2_000, 10_000)     # carded files: under 200, 200-2 000, 2 000-10 000, over 10 000
RELATED_MAX = 3
RELATED_SMALL = 2                           # every source abstained on a drive under DRIVE_SIZE_BANDS[0] files
COMMON_SHARE = 0.3                          # measured on negatives before it is fixed (§25.7)
PROXIMITY_CHARS = 400                       # minimum window of chunk-covered concepts
ABOUT_SHOWN = 10                            # shown per tier (§25.5.6)
MENTIONED_SHOWN = 6
TRANSLATED_SHOWN = 6
LABELLED_SHOWN = 8
CARD_RENDER_MAX = 700
RESULT_CHARS_MAX = 16_000
WINDOW_PAD = 300
WINDOW_MAX = 900
WINDOW_CHARS_TOTAL_V2 = 5_000               # replaces WINDOW_CHARS_TOTAL (§25.5.6)
GROUP_LINES = 6
GROUP_READ_MAX = 1_000
