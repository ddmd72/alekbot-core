"""A worker's bearer key: its record, its format and the shape check (EXTERNAL_TASK_QUEUE_RFC §5.1, §5.6, D8, E6).

A key is `WORKER_KEY_PREFIX` followed by 256 random bits, URL-safe base64 without padding (43 characters). The prefix
makes it routable (the MCP verifier sends a credential with this prefix to the key verifier only) and recognisable to
secret scanners. It is stored nowhere: the record's document id is `keyed_digest(SECRET_DIGEST_KEY,
WORKER_KEY_DIGEST_PURPOSE, key)` and the record keeps only `hint`, the key's last characters. Keys never expire; a
revoked key stays stored with `revoked_at`.

`has_key_shape` runs before any lookup: prefix, exact length and character set, so a malformed key costs no read.
"""
import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict

WORKER_KEY_PREFIX = "alekwk_"              # set at implementation review
WORKER_KEY_RANDOM_BYTES = 32               # 256 bits
WORKER_KEY_RANDOM_CHARS = 43               # unpadded URL-safe base64 of 32 bytes
WORKER_KEY_LENGTH = len(WORKER_KEY_PREFIX) + WORKER_KEY_RANDOM_CHARS
WORKER_KEY_DIGEST_PURPOSE = "external_worker_key"
KEY_HINT_LENGTH = 4

_KEY = re.compile(re.escape(WORKER_KEY_PREFIX) + r"[A-Za-z0-9_-]{%d}" % WORKER_KEY_RANDOM_CHARS)


def has_key_shape(value: str) -> bool:
    """Prefix, exact length and the URL-safe character set; nothing else is looked at."""
    return len(value) == WORKER_KEY_LENGTH and _KEY.fullmatch(value) is not None


def key_hint(key: str) -> str:
    """The non-secret tail of a key that the Cabinet shows."""
    return key[-KEY_HINT_LENGTH:]


class ExternalWorkerKey(BaseModel):
    """`{prefix}external_worker_keys`, doc id `key_digest`."""
    model_config = ConfigDict(frozen=True)

    key_digest: str
    user_id: str
    account_id: str
    worker_id: str
    hint: str
    created_at: datetime
    last_used_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None
