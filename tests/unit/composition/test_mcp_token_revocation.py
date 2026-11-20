"""`/revoke` (RFC 7009) of the facts server: a client revokes its own refresh token over the real SDK routes; another
client's attempt answers 200 and changes nothing; the metadata names the endpoint."""
from tests.unit.composition.test_mcp_oauth_refusals import BASE, World


async def _refresh(http, client, refresh_token):
    return await http.post("/token", data={
        "grant_type": "refresh_token", "refresh_token": refresh_token,
        "client_id": client["client_id"], "client_secret": client["client_secret"]})


async def _revoke(http, client, token):
    return await http.post("/revoke", data={
        "token": token, "client_id": client["client_id"], "client_secret": client["client_secret"]})


async def test_a_client_revokes_its_refresh_token_and_cannot_refresh_with_it():
    w = World()
    async with w.http() as http:
        client = await w.register(http)
        tokens = await w.tokens_for(http, client)

        revoked = await _revoke(http, client, tokens["refresh_token"])
        refreshed = await _refresh(http, client, tokens["refresh_token"])

    assert revoked.status_code == 200
    assert refreshed.status_code == 400 and refreshed.json()["error"] == "invalid_grant"


async def test_revoking_another_clients_token_is_200_and_changes_nothing():
    w = World()
    async with w.http() as http:
        client_a = await w.register(http)
        client_b = await w.register(http)
        tokens = await w.tokens_for(http, client_a)

        revoked = await _revoke(http, client_b, tokens["refresh_token"])
        refreshed = await _refresh(http, client_a, tokens["refresh_token"])

    assert revoked.status_code == 200
    assert refreshed.status_code == 200 and refreshed.json()["access_token"]


async def test_the_metadata_names_the_revocation_endpoint():
    w = World()
    async with w.http() as http:
        meta = await http.get("/.well-known/oauth-authorization-server")

    assert meta.status_code == 200
    assert meta.json()["revocation_endpoint"] == BASE + "/revoke"
