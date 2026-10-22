"""Texts of the deep-research failure notices, one wording for the job and the webhook paths."""


def _topic(query: str) -> str:
    return f'Deep research on "{query.strip() or "the request"}"'


def research_failed_notice(query: str) -> str:
    return f"{_topic(query)} did not complete. Tell the user it failed and that they can ask for it again."


def provider_error_notice(query: str) -> str:
    return (
        f"{_topic(query)} did not complete: the AI provider returned an error. "
        "Tell the user they can ask for it again."
    )


def incomplete_notice(query: str) -> str:
    return (
        f"{_topic(query)} ended incomplete (the provider stopped before finishing). "
        "Tell the user they can ask for it again."
    )


def cancelled_notice(query: str) -> str:
    return f"{_topic(query)} was cancelled before completing. Tell the user they can ask for it again."


def report_not_prepared_notice(query: str) -> str:
    return (
        f"{_topic(query)} finished, but the report could not be prepared. "
        "Tell the user they can ask for it again."
    )
