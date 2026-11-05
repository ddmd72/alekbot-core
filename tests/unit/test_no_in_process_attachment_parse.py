"""U4a-R: email attachments open through the document service only."""
import inspect

from src.ports.email_provider_port import EmailProviderPort

def test_email_provider_port_has_no_deep_download():
    assert "deep" not in inspect.signature(EmailProviderPort.batch_get_full_content).parameters
