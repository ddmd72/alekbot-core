"""Where /auth/callback sends the browser after a successful sign-in: only to a same-site relative path."""
from src.domain.user import UserProfile
from tests.unit.web.test_oauth_app_refusals import World, _set_cookies


async def _sign_in(w: World, next_value: str | None = None, *, planted_cookie: str | None = None):
    await w.seed()
    await w.users.update_user(UserProfile(user_id="user-a", account_id="account-a", external_user_id="google|sub-1"))
    login = await w.client.get("/auth/login" if next_value is None else f"/auth/login?next={next_value}")
    assert login.status_code == 302
    stored = {c.name: c.value for c in w.client.cookie_jar}
    if planted_cookie is not None:  # a cookie set by a pre-fix login, replayed by the browser
        w.client.set_cookie("localhost", "auth_next", planted_cookie)
    resp = await w.client.get(f"/auth/callback?code=c&state={stored['oauth_state']}")
    assert resp.status_code == 302 and "access_token=" in _set_cookies(resp)  # the sign-in itself succeeded
    return resp.headers["Location"]


async def test_external_next_lands_on_the_cabinet():
    assert await _sign_in(World(), "https://evil.example/phish") == "/cabinet"


async def test_protocol_relative_next_lands_on_the_cabinet():
    assert await _sign_in(World(), "//evil.example/phish") == "/cabinet"


async def test_join_link_is_followed():
    assert await _sign_in(World(), "/join?code=X") == "/join?code=X"


async def test_no_next_lands_on_the_cabinet():
    assert await _sign_in(World()) == "/cabinet"


async def test_an_old_cookie_with_an_external_target_is_not_followed():
    target = await _sign_in(World(), "/cabinet", planted_cookie="https://evil.example/phish")
    assert target == "/cabinet"


async def test_login_does_not_store_an_external_next():
    w = World()
    await w.client.get("/auth/login?next=https://evil.example/phish")
    stored = {c.name: c.value for c in w.client.cookie_jar}
    assert stored.get("auth_next") == "/cabinet"
