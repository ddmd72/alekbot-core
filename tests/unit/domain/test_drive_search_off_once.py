"""Drive search off or disconnected is said once per conversation (USER_DRIVE_INDEXING_RFC §10.1 step 0, final review
F5-7). The answer is a per-user fact that does not change within a conversation: the result itself tells Smart not to
search the drive again, instead of a per-user prompt gate."""
import pytest

from src.domain.drive_search import (
    STATUS_DISABLED, STATUS_DISCONNECTED, DriveSearchRequest, DriveSearchResult, render_results,
)

AGAIN = "Do not search the drive again in this conversation"


@pytest.mark.parametrize("status, other_drive", [(STATUS_DISABLED, False), (STATUS_DISCONNECTED, False),
                                                 (STATUS_DISCONNECTED, True)])
def test_the_result_says_search_is_off_for_this_user_and_not_to_retry(status, other_drive):
    text = render_results(DriveSearchResult(status=status, request=DriveSearchRequest(query="lease"),
                                            other_drive=other_drive))
    assert "for this user" in text and AGAIN in text
