"""Cabinet account-level write routes through the Quart client on stores that evaluate (P19): the platform links, the
invite codes and the team join.

Each write is read back through the store; the caller is always the token's user (never a body field); a refusal leaves
the stored documents unchanged, and a positive control keeps a route that refuses everyone from passing.
"""
import pytest

from src.web.cabinet_auth import ACCESS_COOKIE
from tests.support.firestore_eval import eval_transactions
from tests.support.redelivery_world import Rendezvous, require
from tests.unit.web.cabinet_routes_world import ALEX, CLARA, CabinetWorld, race


@pytest.fixture(autouse=True)
def _transactions():
    with eval_transactions():
        yield


@pytest.fixture
async def world():
    w = CabinetWorld()
    await w.seed(ALEX)
    await w.seed(CLARA)
    return w


def _identities(w: CabinetWorld, user_id: str) -> dict:
    return w.user_doc(user_id)["platform_identities"]


# --------------------------------------------------------------------------- /api/user/link-platform

class TestLinkPlatform:
    async def test_the_callers_slack_id_is_stored_on_the_callers_user(self, world):
        resp = await world.client.post("/api/user/link-platform", headers=world.token(ALEX),
                                       json={"platform": "slack", "platform_user_id": "U0ALEX01"})
        assert resp.status_code == 200
        assert _identities(world, "usr-1") == {"slack": "U0ALEX01"}
        assert _identities(world, "usr-2") == {}

    async def test_an_id_held_by_another_user_is_409_and_neither_user_changes(self, world):
        await world.client.post("/api/user/link-platform", headers=world.token(CLARA),
                                json={"platform": "slack", "platform_user_id": "U0CLARA1"})
        before = {u: dict(world.user_doc(u)) for u in ("usr-1", "usr-2")}
        resp = await world.client.post("/api/user/link-platform", headers=world.token(ALEX),
                                       json={"platform": "slack", "platform_user_id": "U0CLARA1"})
        assert resp.status_code == 409
        assert {u: world.user_doc(u) for u in ("usr-1", "usr-2")} == before

    @pytest.mark.parametrize("body", [{}, {"platform": "slack"}, {"platform_user_id": "U0ALEX01"},
                                      {"platform": "", "platform_user_id": ""}])
    async def test_a_missing_field_is_400_and_nothing_is_written(self, world, body):
        before = dict(world.user_doc("usr-1"))
        resp = await world.client.post("/api/user/link-platform", headers=world.token(ALEX), json=body)
        assert resp.status_code == 400
        assert world.user_doc("usr-1") == before

    async def test_a_token_of_a_user_with_no_record_is_400_and_creates_none(self, world):
        ghost = ("usr-9", "acc-9", "ghost@example.com")
        resp = await world.client.post("/api/user/link-platform", headers=world.token(ghost),
                                       json={"platform": "slack", "platform_user_id": "U0GHOST1"})
        assert resp.status_code == 400
        assert "usr-9" not in world.db.collection("users").docs

    @pytest.mark.parametrize("path", ["/api/user/link-platform", "/api/user/link-telegram", "/api/user/join-team"])
    @pytest.mark.parametrize("data", ["{nope", "[1, 2]", "null"])
    async def test_a_body_that_is_not_a_json_object_is_400(self, world, path, data):
        resp = await world.client.post(path, headers={**world.token(ALEX), "Content-Type": "application/json"}, data=data)
        assert resp.status_code == 400

    @pytest.mark.parametrize("platform", ["telegram", "discord"])
    async def test_only_slack_links_here_other_platforms_are_refused(self, world, platform):
        before = dict(world.user_doc("usr-1"))
        resp = await world.client.post("/api/user/link-platform", headers=world.token(ALEX),
                                       json={"platform": platform, "platform_user_id": "not-numeric"})
        assert resp.status_code == 400
        assert world.user_doc("usr-1") == before

    async def test_slack_still_links(self, world):
        resp = await world.client.post("/api/user/link-platform", headers=world.token(ALEX),
                                       json={"platform": "slack", "platform_user_id": "U0ALEX01"})
        assert resp.status_code == 200
        assert _identities(world, "usr-1") == {"slack": "U0ALEX01"}

    async def test_a_phone_cannot_be_linked_without_the_otp_proof(self, world):
        resp = await world.client.post("/api/user/link-platform", headers=world.token(ALEX),
                                       json={"platform": "phone", "platform_user_id": "+12025550142"})
        assert resp.status_code >= 400
        assert "phone" not in _identities(world, "usr-1")


class TestUnlinkPlatform:
    async def test_the_callers_link_is_removed_and_another_users_stays(self, world):
        for who, ident in ((ALEX, "U0ALEX01"), (CLARA, "U0CLARA1")):
            await world.client.post("/api/user/link-platform", headers=world.token(who),
                                    json={"platform": "slack", "platform_user_id": ident})
        resp = await world.client.delete("/api/user/link-platform?platform=slack", headers=world.token(ALEX))
        assert resp.status_code == 200
        assert _identities(world, "usr-1") == {}
        assert _identities(world, "usr-2") == {"slack": "U0CLARA1"}

    async def test_a_missing_platform_is_400_and_the_links_stay(self, world):
        await world.client.post("/api/user/link-platform", headers=world.token(ALEX),
                                json={"platform": "slack", "platform_user_id": "U0ALEX01"})
        resp = await world.client.delete("/api/user/link-platform", headers=world.token(ALEX))
        assert resp.status_code == 400
        assert _identities(world, "usr-1") == {"slack": "U0ALEX01"}


# --------------------------------------------------------------------------- /api/user/link-telegram

class TestLinkTelegram:
    async def test_a_numeric_id_is_stored_trimmed_on_the_callers_user(self, world):
        resp = await world.client.post("/api/user/link-telegram", headers=world.token(ALEX),
                                       json={"telegram_user_id": " 4242001 "})
        assert resp.status_code == 200
        assert _identities(world, "usr-1") == {"telegram": "4242001"}
        assert _identities(world, "usr-2") == {}

    @pytest.mark.parametrize("body, error", [({}, "required"), ({"telegram_user_id": "@alex"}, "numeric"),
                                             ({"telegram_user_id": "12a4"}, "numeric"), ({"telegram_user_id": "-5"}, "numeric")])
    async def test_a_missing_or_non_numeric_id_is_400_and_nothing_is_written(self, world, body, error):
        before = dict(world.user_doc("usr-1"))
        resp = await world.client.post("/api/user/link-telegram", headers=world.token(ALEX), json=body)
        assert resp.status_code == 400 and error in (await resp.get_json())["error"]
        assert world.user_doc("usr-1") == before

    async def test_an_id_held_by_another_user_is_409_and_neither_user_changes(self, world):
        await world.client.post("/api/user/link-telegram", headers=world.token(CLARA), json={"telegram_user_id": "777"})
        before = {u: dict(world.user_doc(u)) for u in ("usr-1", "usr-2")}
        resp = await world.client.post("/api/user/link-telegram", headers=world.token(ALEX), json={"telegram_user_id": "777"})
        assert resp.status_code == 409
        assert {u: world.user_doc(u) for u in ("usr-1", "usr-2")} == before


async def test_the_platforms_read_shows_only_the_callers_links(world):
    await world.client.post("/api/user/link-telegram", headers=world.token(CLARA), json={"telegram_user_id": "777"})
    await world.client.post("/api/user/link-telegram", headers=world.token(ALEX), json={"telegram_user_id": "555"})
    resp = await world.client.get("/api/user/platforms", headers=world.token(ALEX))
    assert (await resp.get_json())["platforms"] == {"telegram": "555"}


# --------------------------------------------------------------------------- /api/user/invite-codes

class TestInviteCodes:
    async def test_an_owner_creates_a_code_stored_for_the_tokens_account_and_user(self, world):
        resp = await world.client.post("/api/user/invite-codes", headers=world.token(ALEX, role="owner"),
                                       json={"type": "team_invite", "email": "clara@example.com", "role": "VIEWER"})
        body = await resp.get_json()
        assert resp.status_code == 201 and body["code"] and body["type"] == "team_invite"
        (stored,) = world.db.collection("invites").docs.values()
        assert (stored["user_id"], stored["account_id"], stored["role"], stored["type"]) == (
            "usr-1", "acc-1", "VIEWER", "team_invite")
        assert body["code"] not in str(stored)                        # shown once, never stored in the clear

    @pytest.mark.parametrize("role", ["member", "viewer"])
    async def test_a_non_owner_is_403_and_no_code_is_stored(self, world, role):
        resp = await world.client.post("/api/user/invite-codes", headers=world.token(ALEX, role=role),
                                       json={"type": "team_invite", "email": "clara@example.com"})
        assert resp.status_code == 403
        assert world.db.collection("invites").docs == {}

    @pytest.mark.parametrize("body, status", [
        ({"type": "team_invite"}, 400),                                                    # no email
        ({"type": "team_invite", "email": "c@example.com", "role": "ADMIN"}, 400),         # unknown role
        ({"type": "self_link", "email": "c@example.com"}, 400),                            # not creatable here
    ])
    async def test_malformed_input_is_4xx_and_no_code_is_stored(self, world, body, status):
        resp = await world.client.post("/api/user/invite-codes", headers=world.token(ALEX), json=body)
        assert resp.status_code == status
        assert world.db.collection("invites").docs == {}

    async def test_the_list_shows_only_the_callers_codes_and_never_a_code_value(self, world):
        mine = await world.client.post("/api/user/invite-codes", headers=world.token(ALEX),
                                       json={"email": "a@example.com"})
        theirs = await world.client.post("/api/user/invite-codes", headers=world.token(CLARA),
                                         json={"email": "b@example.com"})
        resp = await world.client.get("/api/user/invite-codes", headers=world.token(ALEX))
        invites = (await resp.get_json())["invites"]
        assert [i["hint"] for i in invites] == [(await mine.get_json())["hint"]]
        assert (await theirs.get_json())["hint"] not in [i["hint"] for i in invites]
        assert all("code" not in i for i in invites)


# --------------------------------------------------------------------------- /api/user/join-team and /join

async def _invite(world: CabinetWorld, role: str = "MEMBER") -> str:
    resp = await world.client.post("/api/user/invite-codes", headers=world.token(ALEX),
                                   json={"email": "clara@example.com", "role": role})
    return (await resp.get_json())["code"]


class TestJoinTeam:
    async def test_a_whitelisted_user_joins_and_the_code_is_spent(self, world):
        await world.allow("clara@example.com")
        code = await _invite(world, "VIEWER")
        resp = await world.client.post("/api/user/join-team", headers=world.token(CLARA), json={"code": code})
        assert resp.status_code == 200
        assert world.user_doc("usr-2")["account_id"] == "acc-1"
        assert world.db.collection("accounts").docs["acc-1"]["iam_policy"]["usr-2"] == "viewer"
        (stored,) = world.db.collection("invites").docs.values()
        assert stored["used_by_user_id"] == "usr-2"
        again = await world.client.post("/api/user/join-team", headers=world.token(CLARA), json={"code": code})
        assert again.status_code == 400

    async def test_a_user_outside_the_whitelist_is_400_and_nothing_changes(self, world):
        await world.allow("alex@example.com")
        code = await _invite(world)
        accounts_before = {k: dict(v) for k, v in world.db.collection("accounts").docs.items()}
        resp = await world.client.post("/api/user/join-team", headers=world.token(CLARA), json={"code": code})
        assert resp.status_code == 400
        assert world.user_doc("usr-2")["account_id"] == "acc-2"
        assert world.db.collection("accounts").docs == accounts_before
        (stored,) = world.db.collection("invites").docs.values()
        assert "used_by_user_id" not in stored

    @pytest.mark.parametrize("body", [{}, {"code": ""}, {"code": "NOT-A-CODE"}])
    async def test_a_missing_or_unknown_code_is_400_and_nothing_changes(self, world, body):
        await world.allow("clara@example.com")
        resp = await world.client.post("/api/user/join-team", headers=world.token(CLARA), json=body)
        assert resp.status_code == 400
        assert world.user_doc("usr-2")["account_id"] == "acc-2"

    async def test_the_owner_of_the_account_cannot_join_it_again(self, world):
        await world.allow("alex@example.com")
        code = await _invite(world)
        resp = await world.client.post("/api/user/join-team", headers=world.token(ALEX), json={"code": code})
        assert resp.status_code == 400
        (stored,) = world.db.collection("invites").docs.values()
        assert "used_by_user_id" not in stored


class TestJoinKeepsAConcurrentProfileChange:
    async def test_a_join_during_a_timezone_save_keeps_both(self, world):
        """The join writes only the user's `account_id`, so a Cabinet save that read the profile first is kept."""
        await world.allow("alex@example.com")
        invite = await world.client.post("/api/user/invite-codes", headers=world.token(CLARA),
                                         json={"email": "alex@example.com", "role": "MEMBER"})
        code = (await invite.get_json())["code"]
        rv = Rendezvous()
        rv.wrap(world.users, "get_user")
        h = world.token(ALEX)
        joined, saved = await race(
            rv,
            lambda: world.client.post("/api/user/join-team", headers=h, json={"code": code}),
            lambda: world.client.put("/api/user/timezone", headers=h, json={"timezone": "Europe/Lisbon"}))
        require(rv.met and not rv.stalled, "the join and the save read the profile before either wrote it")
        assert (joined.status_code, saved.status_code) == (200, 200)
        stored = world.user_doc("usr-1")
        assert (stored["account_id"], stored["config"]["timezone"]) == ("acc-2", "Europe/Lisbon")


class TestJoinLink:
    async def test_without_a_code_it_is_400(self, world):
        assert (await world.client.get("/join")).status_code == 400

    async def test_a_signed_in_whitelisted_user_joins_and_lands_in_the_cabinet(self, world):
        await world.allow("clara@example.com")
        code = await _invite(world)
        world.client.set_cookie("localhost", ACCESS_COOKIE, world.token(CLARA)["Authorization"][7:])
        resp = await world.client.get(f"/join?code={code}")
        assert (resp.status_code, resp.headers["Location"]) == (302, "/cabinet?joined=true")
        assert world.user_doc("usr-2")["account_id"] == "acc-1"

    async def test_an_anonymous_visitor_goes_to_the_sign_in_with_the_code_and_nothing_is_consumed(self, world):
        code = await _invite(world)
        resp = await world.client.get(f"/join?code={code}")
        assert resp.status_code == 302 and resp.headers["Location"].startswith("/auth/login?next=/cabinet&code=")
        (stored,) = world.db.collection("invites").docs.values()
        assert "used_by_user_id" not in stored

    async def test_a_signed_in_user_outside_the_whitelist_is_sent_to_the_sign_in_and_stays_put(self, world):
        await world.allow("alex@example.com")
        code = await _invite(world)
        world.client.set_cookie("localhost", ACCESS_COOKIE, world.token(CLARA)["Authorization"][7:])
        resp = await world.client.get(f"/join?code={code}")
        assert resp.headers["Location"].startswith("/auth/login")
        assert world.user_doc("usr-2")["account_id"] == "acc-2"
