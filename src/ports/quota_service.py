from abc import ABC, abstractmethod

class QuotaService(ABC):
    """Records usage for an account and announces the day's budget crossing. Advisory: never a gate."""

    @abstractmethod
    async def record_usage(self, account_id: str, model: str, tokens: int, cost: float) -> None:
        """Record `tokens` and `cost` on the account.

        Awaited and durable before it returns (Cloud Run throttles after the response). `model` is a label only.
        Errors: never raises; a repository error is logged and the usage of that call is lost.
        Alert: when this increment takes the account's UTC day from under `daily_cost_limit` to at or over it, one
        post to the alert sink (if any); a failed post is logged. Exactly one increment per day crosses, also
        under concurrent callers. Every writer of usage goes through here, or that day's crossing is never announced
        (P11-G1-4: job_main writes the repository directly).
        """
        pass
