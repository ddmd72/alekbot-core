from unittest.mock import MagicMock, patch

from src.composition.user_agent_factory import UserAgentFactory


def test_factory_wires_the_runner_from_the_switch():
    sentinel = object()
    fake_self = MagicMock()
    ctx = MagicMock()
    with patch("src.composition.user_agent_factory.build_docx_runner", return_value=sentinel) as build:
        agent = UserAgentFactory._build_doc_generator(fake_self, "user1", ctx)
    build.assert_called_once_with()
    assert agent._runner is sentinel
