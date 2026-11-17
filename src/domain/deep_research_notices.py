"""Texts of the deep-research failure notices of the research job."""


def _topic(query: str) -> str:
    return f'Deep research on "{query.strip() or "the request"}"'


def research_failed_notice(query: str) -> str:
    return f"{_topic(query)} did not complete. Tell the user it failed and that they can ask for it again."


def report_not_prepared_notice(query: str) -> str:
    return (
        f"{_topic(query)} finished, but the report could not be prepared. "
        "Tell the user they can ask for it again."
    )
