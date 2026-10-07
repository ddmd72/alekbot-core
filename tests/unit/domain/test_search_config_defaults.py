"""SearchConfig defaults are built per instance — no None state, no shared mutable lists."""
from src.domain.billing import AccountTier
from src.domain.settings import SearchConfig


def test_every_default_is_populated_without_post_init():
    config = SearchConfig()

    assert len(config.DEFAULT_BIOGRAPHICAL_QUERIES) == 3
    assert all(config.DEFAULT_BIO_KEYWORDS_QUERY1)
    assert all(config.DEFAULT_BIO_KEYWORDS_QUERY2)
    assert all(config.DEFAULT_BIO_KEYWORDS_QUERY3)
    for limits in (config.TIERED_SEMANTIC_LIMITS, config.TIERED_BIOGRAPHICAL_LIMITS, config.TIERED_PRINCIPLES_LIMITS):
        assert set(limits) == {AccountTier.FREE, AccountTier.FAMILY, AccountTier.PRO, AccountTier.ENTERPRISE}


def test_mutating_one_instance_does_not_leak_into_the_next():
    first = SearchConfig()
    first.DEFAULT_BIO_KEYWORDS_QUERY1.append("leak")
    first.TIERED_SEMANTIC_LIMITS[AccountTier.FREE] = 999

    second = SearchConfig()

    assert "leak" not in second.DEFAULT_BIO_KEYWORDS_QUERY1
    assert second.TIERED_SEMANTIC_LIMITS[AccountTier.FREE] == 20
