from abc import ABC, abstractmethod


class AlertSinkPort(ABC):
    """Outbound channel for operational alerts (budget, provider failover, …).

    Deliberately dumb: one plain-text message, no severity, no routing. Callers that
    need structure format it themselves — keeping the port this narrow is what lets
    services depend on it without importing a concrete webhook adapter (REQ-ARCH-22).

    Implementations MUST be best-effort from the caller's point of view: an alert that
    fails to deliver may raise, but no caller should let that break its own work.
    """

    @abstractmethod
    async def post(self, text: str) -> None:
        """Deliver one alert message."""
        pass
