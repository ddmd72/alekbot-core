"""Sandbox startup self-check (UNTRUSTED_CONTENT_SANDBOX_RFC §4.3, I1)."""
from src.domain.sandbox_errors import SandboxMisconfigured, SandboxUnavailable
from src.domain.sandbox_startup_check import check_sandbox_environment

CLEAN = {
    "APP_ENV": "dev", "PORT": "8080", "K_SERVICE": "svc", "SANDBOX_URL": "u",
    "SANDBOX_RECYCLE": "1", "PATH": "/usr/bin",
}


def test_clean_environment_passes() -> None:
    assert check_sandbox_environment(CLEAN, frozenset()) == []


def test_secret_like_name_flagged() -> None:
    for name in ("API_KEY", "X_TOKEN", "DB_PASSWORD", "MY_SECRET", "CREDENTIALS"):
        assert check_sandbox_environment({**CLEAN, name: "v"}, frozenset()) == [name]


def test_google_application_credentials_flagged() -> None:
    env = {**CLEAN, "GOOGLE_APPLICATION_CREDENTIALS": "/x.json"}
    assert "GOOGLE_APPLICATION_CREDENTIALS" in check_sandbox_environment(env, frozenset({"GOOGLE_APPLICATION_CREDENTIALS"}))


def test_allowlisted_name_passes_even_if_it_matches() -> None:
    env = {**CLEAN, "SOME_TOKEN_LIMIT": "5"}
    assert check_sandbox_environment(env, frozenset({"SOME_TOKEN_LIMIT"})) == []


def test_violations_never_contain_values() -> None:
    out = check_sandbox_environment({"API_KEY": "hunter2"}, frozenset())
    assert out == ["API_KEY"] and "hunter2" not in "".join(out)


def test_errors() -> None:
    assert SandboxUnavailable.charges_file is False
    assert issubclass(SandboxMisconfigured, Exception)
