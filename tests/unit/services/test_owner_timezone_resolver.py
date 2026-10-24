"""Facts S4 · T7c: `OwnerTimezoneResolver`, the one place the owner's zone is read (S2+S3 hexagonal review M5).

The account and user stores are the fakes with semantics of the consolidation owner-zone tests: they hold documents,
record which ids were read and can be made to raise.
"""
import logging
from typing import List

import pytest

from src.domain.owner_timezone import FALLBACK_TIMEZONE, ResolvedOwnerZone
from src.services.owner_timezone_resolver import OwnerTimezoneResolver
from tests.unit.services.test_consolidation_owner_timezone import ACCOUNT, Accounts, Users, account, profile

PARIS = "Europe/Paris"
TOKYO = "Asia/Tokyo"
UNRESOLVED = "owner timezone unresolved for account"


def warnings(caplog) -> List[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


async def test_the_owner_is_found_and_their_zone_returned(caplog):
    accounts, users = Accounts(account(ann="owner", bob="member")), Users(profile("ann", PARIS), profile("bob", TOKYO))

    with caplog.at_level(logging.WARNING):
        resolved = await OwnerTimezoneResolver(accounts, users).resolve(ACCOUNT)

    assert resolved == ResolvedOwnerZone("ann", PARIS)
    assert accounts.asked == [ACCOUNT] and users.asked == ["ann"]
    assert warnings(caplog) == []


async def test_no_account_gives_no_owner_utc_and_one_warning(caplog):
    users = Users(profile("ann", PARIS))

    with caplog.at_level(logging.WARNING):
        resolved = await OwnerTimezoneResolver(Accounts(), users).resolve(ACCOUNT)

    assert resolved == ResolvedOwnerZone(None, FALLBACK_TIMEZONE) and FALLBACK_TIMEZONE == "UTC"
    assert users.asked == []
    (found,) = warnings(caplog)
    assert UNRESOLVED in found and ACCOUNT[:8] in found and "account not found" in found and found.endswith("; using UTC")


async def test_an_account_without_an_owner_gives_no_owner_utc_and_one_warning(caplog):
    users = Users(profile("bob", TOKYO))

    with caplog.at_level(logging.WARNING):
        resolved = await OwnerTimezoneResolver(Accounts(account(bob="member")), users).resolve(ACCOUNT)

    assert resolved == ResolvedOwnerZone(None, "UTC")
    assert users.asked == []
    (found,) = warnings(caplog)
    assert UNRESOLVED in found and "account has no owner" in found


async def test_several_owners_give_the_first_ones_zone_and_one_warning(caplog):
    accounts = Accounts(account(zed="owner", ann="owner"))
    users = Users(profile("ann", TOKYO), profile("zed", PARIS))

    with caplog.at_level(logging.WARNING):
        resolved = await OwnerTimezoneResolver(accounts, users).resolve(ACCOUNT)

    assert resolved == ResolvedOwnerZone("ann", TOKYO)
    assert users.asked == ["ann"]
    (found,) = warnings(caplog)
    assert "2 owners" in found and ACCOUNT[:8] in found and UNRESOLVED not in found


@pytest.mark.parametrize("zone", ["Mars/Olympus", ""])
async def test_a_bad_zone_gives_utc_and_one_warning_and_keeps_the_owner(zone, caplog):
    with caplog.at_level(logging.WARNING):
        resolved = await OwnerTimezoneResolver(Accounts(account(ann="owner")), Users(profile("ann", zone))).resolve(ACCOUNT)

    assert resolved == ResolvedOwnerZone("ann", "UTC")
    (found,) = warnings(caplog)
    assert UNRESOLVED in found and found.endswith("; using UTC")


async def test_an_owner_without_a_profile_gives_utc_and_one_warning(caplog):
    with caplog.at_level(logging.WARNING):
        resolved = await OwnerTimezoneResolver(Accounts(account(ann="owner")), Users()).resolve(ACCOUNT)

    assert resolved == ResolvedOwnerZone("ann", "UTC")
    (found,) = warnings(caplog)
    assert "owner has no profile" in found


async def test_a_raising_account_store_gives_utc_and_one_warning_never_a_raise(caplog):
    accounts = Accounts(account(ann="owner"), raises=RuntimeError("firestore unavailable"))
    users = Users(profile("ann", PARIS))

    with caplog.at_level(logging.WARNING):
        resolved = await OwnerTimezoneResolver(accounts, users).resolve(ACCOUNT)

    assert resolved == ResolvedOwnerZone(None, "UTC")
    assert users.asked == []
    (found,) = warnings(caplog)
    assert UNRESOLVED in found and "RuntimeError: firestore unavailable" in found


async def test_a_raising_user_store_gives_utc_and_one_warning_and_keeps_the_owner(caplog):
    users = Users(raises_for={"ann": TimeoutError("deadline")})

    with caplog.at_level(logging.WARNING):
        resolved = await OwnerTimezoneResolver(Accounts(account(ann="owner")), users).resolve(ACCOUNT)

    assert resolved == ResolvedOwnerZone("ann", "UTC")
    (found,) = warnings(caplog)
    assert UNRESOLVED in found and "TimeoutError: deadline" in found


async def test_each_resolve_reads_again():
    accounts, users = Accounts(account(ann="owner")), Users(profile("ann", PARIS))
    resolver = OwnerTimezoneResolver(accounts, users)

    await resolver.resolve(ACCOUNT)
    users.held["ann"] = profile("ann", TOKYO)

    assert (await resolver.resolve(ACCOUNT)).zone == TOKYO
    assert accounts.asked == [ACCOUNT, ACCOUNT]
