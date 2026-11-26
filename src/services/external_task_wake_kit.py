"""WakeKit — the three collaborators of the external task queue's wake-up, passed to `ExternalTaskService` as one
(EXTERNAL_TASK_QUEUE_RFC §5.6, §5.8).

They serve the wake-up only and are always given together: the stored-secrets cipher (a target is stored only as one
ciphertext), the outbound-request guard that sends the ping, and the bot's own host names the guard refuses (lowercase).
Without the cipher or the guard no target can be stored (`wake_unavailable`) and no ping is sent; the default kit has
neither. One bundle keeps the service's constructor within the composition arity cap.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Collection, Optional

from ..ports.cipher_port import CipherPort
from ..ports.outbound_webhook_port import OutboundWebhookPort


@dataclass(frozen=True)
class WakeKit:
    cipher: Optional[CipherPort] = None
    webhook: Optional[OutboundWebhookPort] = None
    own_hosts: Collection[str] = ()
