"""Why a wake-up POST to a worker's target was not delivered: one closed set of codes (EXTERNAL_TASK_QUEUE_RFC §5.8).

One module on purpose: the code enum and the exception that carries it at save time are read together by every caller.

The codes are the only thing about a failed wake-up that ever reaches the owner (the Cabinet's error, the refusal
notice); never an HTTP reason phrase, a response body, the URL or a header. Two kinds:
- final (the RFC's refusal list): the guard refused the target (`scheme`, `port`, `name`, `own_host`, `internal_name`,
  `address`, `dns_failure`, `header`), or the target answered in a way a retry cannot change (`redirect`, any 3xx;
  `client_error`, any 4xx or other non-2xx answer). The wake task answers 200 and posts one notice;
- retryable: the target is down (`server_error`, a 5xx) or did not answer in time (`unreachable`: a connect, TLS or
  read failure, or the whole send outran `WAKE_TIMEOUT_S`). The wake task answers 5xx and Cloud Tasks retries.
"""
from enum import Enum


class WakeRefusal(str, Enum):
    SCHEME = "scheme"
    PORT = "port"
    NAME = "name"
    OWN_HOST = "own_host"
    INTERNAL_NAME = "internal_name"
    ADDRESS = "address"
    DNS_FAILURE = "dns_failure"
    REDIRECT = "redirect"
    CLIENT_ERROR = "client_error"
    HEADER = "header"
    SERVER_ERROR = "server_error"
    UNREACHABLE = "unreachable"

    @property
    def is_final(self) -> bool:
        """False only for the two outcomes a retry may change."""
        return self not in (WakeRefusal.SERVER_ERROR, WakeRefusal.UNREACHABLE)


class WakeTargetRefused(Exception):
    """A wake-up target the guard refused at save time; nothing was stored. `refusal` says why."""

    def __init__(self, refusal: WakeRefusal) -> None:
        super().__init__(refusal.value)
        self.refusal = refusal
