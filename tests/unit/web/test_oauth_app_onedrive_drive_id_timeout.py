"""The drive id read at connect has its own timeout (USER_DRIVE_INDEXING_RFC §10.1 step 0): it is best effort, so a
hung Graph call must not hold the OAuth callback until the session default (5 minutes) runs out."""
import aiohttp

from tests.unit.web.test_oauth_app_onedrive_drive_id import _TOKEN, _callback, _resp, _session, oauth_port  # noqa: F401


async def test_the_app_folder_read_carries_a_ten_second_timeout(oauth_port):  # noqa: F811
    session = _session(_resp(_TOKEN), _resp({"parentReference": {"driveId": "d1"}}))
    await _callback(oauth_port, session)
    timeout = session.get.call_args.kwargs["timeout"]
    assert isinstance(timeout, aiohttp.ClientTimeout) and timeout.total == 10


async def test_a_timeout_still_connects_without_a_drive_id(oauth_port):  # noqa: F811
    resp = await _callback(oauth_port, _session(_resp(_TOKEN), TimeoutError()))
    assert resp.headers["Location"] == "/cabinet?drive_connected=1"
    assert oauth_port.save.call_args.args[0].drive_id == ""
