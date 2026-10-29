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
SYSTEMIC_BACKOFF_BASE_S = 60                # continuation delay after a systemic error, doubled per failure
SYSTEMIC_BACKOFF_CAP_S = 1800               # = 3 x TICK_INTERVAL
RECORD_WRITE_BATCH = 400
KEYED_DELETE_BATCH = 50                     # chunks and hints per delete commit: a delete's index entries count toward
                                            # Firestore's transaction size, and a v2 chunk carries up to 900 keys
                                            # (400 failed the first purge of a v2 index, "Transaction too big")
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
MAX_PDF_DOWNLOAD_BYTES = 100 * 1024 * 1024  # indexer only; chat/open keeps MAX_DRIVE_DOWNLOAD_BYTES
                                            # (decisions/drive_indexer_pdf_download_cap.md)
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
QUERY_TERM_CHARS = 1000                     # the search extracts query terms from at most this many characters of
                                            # drive_query, cut at a word boundary (no length cap upstream; arbiter R2 S-m1)
MODEL_EXTRACT_DENSITY = 200                 # chars per page below which a PDF is routed to the reader (§9)
MODEL_EXTRACT_GARBAGE = 0.05                # share of unreadable characters above which it is routed
MODEL_EXTRACT_MAX_PAGES = 200
MODEL_PAGES_PER_CALL = 10                   # far below the 100k-token price step (§24.3)
MODEL_IN_FLIGHT = 4
TRIAGE_BATCH = 8                            # Search v2 (§25.4.3): enriched triage, 8 photos per call (was 16)
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
# (TRIAGE_BATCH was replaced by P3). Retired v1 names (§25.12) stay until their readers go.
PROFILE_MIN_CHARS = 200                     # under it a note gets a template profile and no chunk/hint score (§25.3)
PROFILE_REFRESH_COOLDOWN = timedelta(hours=24)
PASSAGE_REFRESH_COOLDOWN = timedelta(hours=24)
PASSAGE_CHUNKS_PER_CALL = 2
PASSAGE_MAX_CHUNKS_PER_FILE = 100           # ≈ 240 000 chars of a file get passage hints (D17 acknowledgement)
PASSAGE_REUSE_SHARE = 0.8                   # share of a new chunk's paragraphs from one old chunk to reuse its hints
PASSAGE_MIN_SIMILARITY = 0.5                # calibrated (§25.7)
TRIAGE_BATCH_V2 = TRIAGE_BATCH              # P3 replaced TRIAGE_BATCH with the v2 value (§25.4.3)
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
WORD_KEYS_PER_CHUNK = 1_500                   # key caps (§25.2.6)
HINT_KEYS_PER_CHUNK = 60
ABOUT_KEYS_PER_RECORD = 600
WRITE_BATCH_BYTES = 6_000_000               # bounds request + index-entry bytes of a write batch (Firestore counts both), besides RECORD_WRITE_BATCH
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
WINDOW_CHARS_TOTAL_V2 = 5_000               # the v1 12 000 is gone (§25.5.6)
GROUP_LINES = 6
GROUP_READ_MAX = 1_000
# model output caps the schemas, verification, grounding and the keyer share (§25.2.6, §25.4.1-§25.4.3; review M6)
PROFILE_ENTITIES_MAX = 12
ENTITY_FORMS_MAX = 8
PROFILE_KEY_VALUES_MAX = 12
PROFILE_DATES_MAX = 5
PROSE_NUMBERS_MAX = 12                      # graded number terms of title + what_it_is (§26.4; a limit on cost, not a weight)
PROFILE_TOPICS_MAX = 8                      # and as many topics_native
PROFILE_WORD_LIST_MAX = 24                  # each of topic_words, label_words, domain_words, validity_words
HINT_KEYWORDS_MAX = 12                      # per chunk
HINT_QUESTIONS_MAX = 3
HINT_EVIDENCE_WORDS_MAX = 12
PHOTO_DESCRIPTION_CHARS = 240
PHOTO_OBJECTS_MAX = 8
PHOTO_OBJECT_FORMS_MAX = 6
PHOTO_SCENE_WORDS = 6
PHOTO_SCENE_LABELS_MAX = 6                  # D40: scene labels a photograph keeps
PHOTO_VISIBLE_TEXT_CHARS = 200
# profile input slices (§25.3): cut at a paragraph or sentence boundary
PROFILE_HEAD_CHARS = 6_000                  # also JSON's head and the sheets' rows cap
PROFILE_TAIL_CHARS = 1_500
PROFILE_HEADINGS_CHARS = 1_500              # heading lines, only over PROFILE_HEADINGS_MIN_TEXT chars of text
PROFILE_HEADINGS_MIN_TEXT = 20_000
PROFILE_EPUB_BODY_CHARS = 3_000
PROFILE_SHEET_ROWS = 20                     # first rows per sheet
README_EXCERPT_HEAD_CHARS = 6_000           # a collapsed folder's readme_excerpt
README_EXCERPT_TAIL_CHARS = 1_500
README_CHILD_NAMES = 200
PHRASE_RUN_MAX = 3                          # query phrase runs of 1-3 tokens give `e:` keys (§25.5.1)
PROFILER_TIMEOUT_S = 60                     # model call timeouts (§25.4.4)
PASSAGES_TIMEOUT_S = 60
TRIAGE_TIMEOUT_S = 120
# P5 (indexer integration): per-unit cost estimates at the primary's price (§25.4.5 "a file starts only if its
# expected cost fits", §25.6's cost table; the page and image counts of §24.7 are estimates now, not caps)
PROFILE_USD_ESTIMATE = 0.0008               # one profile call
PASSAGE_CHUNK_USD_ESTIMATE = 0.0004         # one chunk's passage hints
READER_PAGE_USD_ESTIMATE = 0.0006           # one page the reader reads (§24.9)
TRIAGE_IMAGE_USD_ESTIMATE = 0.0003          # one enriched-triage image (§24.9 + §25.6's +$0.000125)
# The pages a whole month's reader cap pays at the per-page estimate (§25.4.5; review rv_sv2_p5 MINOR 4): a file that
# needs more is cut to it rather than waiting for a month that could never pay for it, and the reader is never asked
# for more in one file. Derived from the dollar cap: the §24.7 page cap (5 000) and image cap are retired.
MODEL_EXTRACT_PAGES_PER_MONTH = int(READER_USD_PER_MONTH / READER_PAGE_USD_ESTIMATE + 1e-9)
ENRICH_QUEUE_PAGE = 30                      # enrichment queue records read at once (§25.4.5)
ENRICH_WAKEUPS_PAGE = 100                   # deferred or parked jobs re-planned per tick
DESCRIBED_STALL_AFTER = timedelta(hours=24) # a rebuild whose described count does not grow this long alerts (§25.8)

# -- files the bot cannot open (§26.2, §26.12; D33) ----------------------------------------------------------------
INACCESSIBLE_RETRY_MIN = timedelta(hours=1)      # Δ51: an observation of the item or its parent makes it due, at most
                                                 # this often (delta re-emits unchanged items often)
INACCESSIBLE_RETRY_SLOW = timedelta(days=7)      # OQ-C1 (§26.11): whether a permission change alone re-emits an item
                                                 # in the App Folder's delta is not verified, so a marked item is also
                                                 # re-read on a timer, one item call each
INACCESSIBLE_NOTICE_BATCH = 2_000                # files in one notice; the rest on the next pass
INACCESSIBLE_SCAN_MAX = 2_000                    # marked records one read returns (the tick's counters, the fan-out)
SUBFOLDER_SCAN_MAX = 400                         # stored records read to mark a closed folder's sub-folders with it

# -- capture (§26.3, §26.12; D34) -----------------------------------------------------------------------------------
FULL_PAGE_COVER = 0.95                           # rule 2: a page's one raster covers at least this share of the page
SCAN_DPI_MIN = 150                               # rule 2: a scanner's effective resolution (200 dpi observed, §26.1)
SCAN_DPI_MAX = 600
SCAN_GLASS_MAX_MM = (220, 360)                   # rule 2: (short, long) side of a scanner glass: A4, Letter and Legal
CAPTURE_PAGES_MAX = 50                           # the parser child inspects at most the first this many pages for the
                                                 # capture signals: "every page" rules hold on the pages inspected
CAPTURE_BUDGET_S = 5.0                           # the signals walk's own time budget in the child; out of it: no signals

# -- transcripts (§27, §27.18; D41-D46) ------------------------------------------------------------------------------
# Initial values; the T1 eval gate may revise them. Each comment names the RFC basis.
TRANSCRIBE_USD_PER_MONTH = 20.0             # D46: the month's hard cap; ~125 audio hours at the post-promotion $0.16/h
TRANSCRIBE_HOUR_USD_ESTIMATE = 0.30         # per audio hour, reserved at submit; bounds a window's worst case: audio input + the per-window output cap at the held batch price (T-R19)
TRANSCRIBE_WINDOW_PROMPT_TOKENS = 2_000     # per window, on top of the hour rate: system instruction + task text (~1 130 tokens measured), rounded up (T-R19 N2)
TRANSCRIBE_MIN_OUTPUT_TOKENS = 4_096         # floor of a window's output cap: thinking tokens count toward the output cap, so a short window needs headroom (T-R26)
TRANSCRIBE_AUDIO_TOKENS_PER_S = 25          # audio input tokens per second of audio, for the worst-case estimate (T-R19, T-R26)
TRANSCRIBE_WINDOW_S = 1_800                 # §27.4: 30-45 min allowed (Δ129); keep <= 45 min: parse_stamp is strict M:SS below one hour
TRANSCRIBE_OVERLAP_S = 60                   # §27.6, Δ108: both windows hold a few utterances of every speaker
TRANSCRIBE_COVERAGE_MIN = 0.9               # Δ129: last segment start / window duration below it = failed attempt
TRANSCRIBE_COVERAGE_TAIL_S = 60             # T-R31: the coverage tolerance is max((1 - COVERAGE_MIN) x d, this); a short window's quiet tail passes
TRANSCRIBE_SHORT_ACCEPT_MAX_S = 300         # T-R31: a second clean (STOP) short answer is accepted as it is up to this window length; a longer one is accepted marked `truncated`
TRANSCRIBE_MAX_SECONDS = 14_400             # Δ100: 4 h, the cost bound and the 1 MiB document bound
TRANSCRIBE_MAX_BYTES = 8 * 1024 ** 3        # Δ100: 8 GiB, checked from metadata before any ffprobe
TRANSCRIBE_EXTRACT_WALL_S = 240             # §27.4: one extraction step's wall (~32 CPU-s per 30-min window plus network)
MEDIA_PASS_MAX_WINDOWS = 3                  # T-R15: windows cut from one remote read of a file without a duration
TRANSCRIBE_CHILD_CPU_S = 150                # §27.4: the media child's RLIMIT_CPU
TRANSCRIBE_SHARE = 0.25                     # §27.3: start window of the extraction stage
TRANSCRIBE_REREAD_COOLDOWN = timedelta(hours=24)   # an edited recording is re-bought at most this often, as the reader
BATCH_POLL_FIRST_S = 150                    # Δ133: T0 saw 2-5 min of queue latency
BATCH_POLL_EVERY_S = 300                    # Δ133
BATCH_POLLS_PER_TICK = 10                   # §27.5: one poll per distinct batch, at most this many a tick
BATCH_SLOW_AFTER_S = 3_600                  # §27.5: a batch older than this drops to the slow cadence
BATCH_POLL_SLOW_S = 1_800                   # §27.5: the slow poll interval (the API's contract is 24 h)
BATCH_SUBMIT_LOOKUPS = 3                    # T-R28: a `submitting` batch the provider does not list after this many lookups ...
BATCH_SUBMIT_LOOKUP_MIN_AGE_S = 900         # ... and at least this old was never created: its windows and reservation go back
BATCH_ERROR_BACKOFF_S = 1_800               # a batch whose handling raised (not an outage) is polled again after this, the others go on
TRANSCRIBE_MAX_GAP_S = 75                   # D43: "once a minute"; a longer gap between a speaker's stamps is interpolated
INTERPOLATED_MAX_SHARE = 0.3                # §27.6: a window above this share of interpolated stamps is resubmitted once
STITCH_MIN_MATCHED_WORDS = 6                # §27.6: a seam mapping with fewer matched words is marked uncertain
SUMMARY_MAX_CHARS = 1_200                   # §27.7
SUMMARY_INPUT_CHARS = 60_000                # §27.7
TRANSCRIPT_DOC_BYTES = 900_000              # Firestore's 1 MiB document with margin; over it the store is sharded
TRANSCRIPT_ORPHAN_TTL = timedelta(days=30)  # §27.8 b: a source restored from the recycle bin within the window
MIRROR_OPS_PER_TICK = 40                    # §27.9: Graph writes per tick
MIRROR_SCAN_PAGE = 100                      # §27.9: a rotating drift scan
MIRROR_AUDIT_DAYS = 7                       # §27.9: worst-case time to undo a user's edit
MIRROR_NAME_MAX = 240                       # OneDrive's 255-name limit with margin
ONEDRIVE_PATH_MAX = 400                     # Δ193: OneDrive's decoded path limit, counted from the drive root
MIRROR_PATH_MARGIN = 10                     # Δ193: a mirror path must fit ONEDRIVE_PATH_MAX - this
MIRROR_READS_PER_TICK = 10                  # §27.9.5: Graph reads of the mirror step, its own bucket
MIRROR_STAMPS_PER_TICK = 200                # §27.9.5: store reads and renders that end in a stamp with no Graph write
MIRROR_SEED_PAGE = 200                      # §27.9.4: records per seed page
MIRROR_ARCHIVE_GRACE = timedelta(hours=1)   # §27.9.6: a deletion mark waits this long before the archive move
MIRROR_ROOT_GRACE = timedelta(hours=24)     # §27.9.9: a lost root is graced this long before the loss is confirmed
MIRROR_RESERVE_S = 45                       # §27.9.5: tail of the start budget kept for the mirror step
MIRROR_MAX_ATTEMPTS = 5                     # §27.9.5: a failing mirror action is charged this many times
MIRROR_FAILED_RETRY = timedelta(days=7)     # §27.9.5: a failed document is tried again after this
MIRROR_QUOTA_HOLD = timedelta(hours=6)      # §27.9.5: a full drive holds the mirror step this long
MIRROR_CLOCK_SKEW = timedelta(minutes=5)    # Δ210, Δ217: Graph's created time against the indexer's, on an intent re-run
ARCHIVE_KEYS_MAX = 12_000                   # §27.9.11: cap of archive_keys on a ledger document
ARCHIVE_QUERY_MAX = 100                     # §27.9.11: documents per deleted-files driver query
DELETED_SHOW_MAX = 5                        # §27.9.11: archived transcripts shown by one search
TRANSCRIBE_UPLOAD_S = 60                    # G16: worst case of uploading one window; added to the tick deadline sum
TRANSCRIBE_FILE_LIFE_AT_SUBMIT = timedelta(hours=24)   # §27.5: an uploaded window is submitted only with this much of its provider file's life left (the batch API's 24 h contract); less is re-extracted first
TRANSCRIBE_MIN_RUN_WALL_S = 90              # Task 12: a further extraction run of a file within one step starts only with this much of the step's wall left (one 30-min window needs ~32 CPU-s plus its bytes)
TRANSCRIPT_OPEN_MAX_CHARS = 60_000          # G19: not an RFC name; the most an open returns, the summary input bound
# DRIVE_FILE_NOTES_RFC §5.4 (N1): the owner note's own key cap, classifiers first and never cut. Defined here so the
# keyer (`drive_keys`) can read it without importing `drive_note` (which imports the keyer); `drive_note` re-exports it.
# Deliberately NOT in `drive_keys.index_tables_digest`: it shapes only noted records, which NOTE_MERGE_VERSION re-merges.
NOTE_KEYS_MAX = 320
# §28.9 answer from content: the drive specialist's passages (find_passages, read_passages). A folder ranks in memory
# up to FOLDER_INMEM_CHUNKS, as search's folder path does (plan D-P2).
EXCERPTS_K = 10                             # new chunks per find
EXCERPTS_CHARS_MAX = 24_000                 # characters per call: passages are content, not RESULT_CHARS_MAX
EXCERPTS_PER_FILE_MAX = 8                   # leaves room for a second file's best chunk
EXCERPTS_LEXICAL_WINDOW = 30                # the vector-nearest chunks whose word keys vote (A4)
EXCERPTS_RRF_K = 60                         # the k of src/domain/rrf.py; fractional ranks, so computed apart (D-P3)
EXCERPT_RANGE_MAX = 10                      # chunks per read_passages
EXCERPT_FILE_REFS_MAX = 10                  # files per scoped find
CARD_ACCURACY_TRUST = 90                    # OQ-A2: a value is trusted from a card at this accuracy or more
# §28.3 A18, OQ-A3: the "where it comes from" lines of passages. The cap counts folder notes only, a guard against a
# deep tree of long notes; a file's own note and description sit outside it.
EXCERPT_FOLDER_NOTES_CHARS_MAX = 4_000
EXCERPT_DESCRIPTION_CHARS = 200             # a file's what-it-is line, cut at a word boundary
