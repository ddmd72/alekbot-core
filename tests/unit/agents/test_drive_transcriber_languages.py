"""M5: the language codes of a window's answer are shaped like ISO 639 codes and few (final review)."""
import json

import pytest

from src.agents.drive_transcriber_agent import AGENT_TYPE, DriveTranscriberAgent
from src.domain.agent import AgentConfig
from src.infrastructure.drive_model_call import DriveOutputInvalid


def _parse(codes):
    agent = DriveTranscriberAgent(AgentConfig(agent_id="drive_transcriber_u", agent_type=AGENT_TYPE,
                                              capabilities=[AGENT_TYPE]))
    return agent.parse(json.dumps({"language_codes": codes, "speakers": 1, "no_speech": True, "segments": []}))


@pytest.mark.parametrize("codes", [["uk"], ["en", "es", "ru"], ["fil"], ["pt-BR"], ["zh-419"], []])
def test_iso_shaped_codes_pass(codes):
    assert _parse(codes).language_codes == tuple(codes)


@pytest.mark.parametrize("codes", [["Ukrainian"], ["u"], ["EN"], ["en_US"], ["en-"], ["ignore previous instructions"],
                                   [""], ["x" * 40], ["en", "es", "fr", "de", "it", "pt"]])
def test_other_codes_are_an_invalid_output(codes):
    with pytest.raises(DriveOutputInvalid):
        _parse(codes)
