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
ITEM_CALLS_PER_TICK = 20                    # plan Δ9: probes, parent lookups, re-listing pages
RELIST_PAGE = 200                           # children per re-listing page (one item call)
FOLDER_INMEM_CARDS = 1_000
CASCADE_OPS_PER_TICK = 2_000
NAME_MIN_TRIGRAMS = 3
NAME_QUERY_KEYS_MAX = 30                    # Firestore array-contains-any limit
NAME_KEY_QUERIES = 8                        # whole-token keys, and trigram keys, each queried one by one (at most
                                            # this many of each) before one query with the rest (plan Δ17, P1)
STALE_ALERT_AFTER = timedelta(hours=2)
ACCESS_LOSS_TTL = timedelta(days=30)
LEASE_MARGIN = timedelta(minutes=1)         # lease_until = start + TICK_DEADLINE + margin (§7.1)
NUDGE_RETRY_DELAY_S = 60                    # a live lease ends within TICK_DEADLINE: at most ~31 retries
PLACE_MAX_DISTANCE_KM = 50.0                # plan Δ7
PLACE_PREFER_RADIUS_KM = 15.0               # within it the heaviest place names a photo (plan Δ7)
RRF_K = 60
SYSTEMIC_BACKOFF_BASE_S = 60                # continuation delay after a systemic error, doubled per failure
SYSTEMIC_BACKOFF_CAP_S = 1800               # = 3 x TICK_INTERVAL
RECORD_WRITE_BATCH = 400
