"""AlertSinkPort contract: the port's shape and the Slack webhook adapter that implements it."""

import pytest

from src.ports.alert_sink import AlertSinkPort


class TestAlertSinkPortContract:

    def test_post_is_abstract(self):
        assert getattr(AlertSinkPort.post, "__isabstractmethod__", False)

    def test_cannot_instantiate_port(self):
        with pytest.raises(TypeError):
            AlertSinkPort()

    def test_slack_webhook_adapter_implements_the_port(self):
        from src.adapters.slack.webhook_adapter import SlackWebhookAdapter

        assert issubclass(SlackWebhookAdapter, AlertSinkPort)
        assert isinstance(SlackWebhookAdapter("https://example.invalid"), AlertSinkPort)
