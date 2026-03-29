import aiohttp

from ...utils.logger import logger


class SlackWebhookAdapter:
    """Sends messages to a Slack channel via incoming webhook URL."""

    def __init__(self, webhook_url: str) -> None:
        self._webhook_url = webhook_url

    async def post(self, text: str) -> None:
        async with aiohttp.ClientSession() as session:
            resp = await session.post(self._webhook_url, json={"text": text})
            if resp.status != 200:
                body = await resp.text()
                logger.error(
                    f"[SlackWebhookAdapter] delivery failed: status={resp.status} body={body}"
                )
                raise RuntimeError(f"Slack webhook error {resp.status}: {body}")
