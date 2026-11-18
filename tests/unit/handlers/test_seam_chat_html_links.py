"""P11-G4-5 (owner decision O8) on a Slack chat turn: an html page the turn returns reaches the user as their `/f/<token>`
capability link, never as its storage key and never dropped.

The real `ConversationHandler` on the seam world (tests/support/seam_world.py) with Slack's `RichContentService` as
`notification_setup.build_notifications` builds it: the media bucket (the real `GcsMediaAdapter` over the GCS fake) and
the world's `FileLinkService`. The Router stand-in answers with what Smart returns: an html `file` rich-content item, or
an `html_gcs_link` delivery item (`web_search_agent.py`). The chat channel is the recording `ChatChannel`.
"""
from typing import List

from src.domain.agent import AgentMessage, AgentResponse, DeliveryItem
from src.domain.messaging import RichContent, SmartResponse
from src.services.file_access_token_service import FileAccessTokenService
from src.services.rich_content_service import RichContentService
from tests.support.recording_task_queue import SERVICE_URL
from tests.support.seam_world import BUCKET, OTHER_SLACK, SECRET, USER, ChatChannel, SeamWorld, require, slack_turn

PAGE = "<html><head></head><body>Heat pumps in Springfield</body></html>"


def _slack_rich_content(world: SeamWorld) -> RichContentService:
    return RichContentService(media_port=world.media["slack"], storage_port=world.media_store,
                              link_service=world.links)


def _answers(text: str, *, rich: RichContent = None, items: List[DeliveryItem] = ()):  # type: ignore[assignment]
    def respond(message: AgentMessage) -> AgentResponse:
        return AgentResponse.success(task_id=message.task_id, agent_id=message.recipient,
                                     result=SmartResponse(text=text, structured_data=rich),
                                     delivery_items=list(items))
    return respond


def _links_in(texts: List[str]) -> List[str]:
    return [t for t in texts if f"{SERVICE_URL}/f/" in t]


def _key_of(world: SeamWorld, text: str) -> str:
    """The object key the link's capability token names, after checking the token is the user's."""
    token = text.split(f"{SERVICE_URL}/f/", 1)[1].split("|", 1)[0].rstrip(">")
    access = FileAccessTokenService(SECRET).verify(token)
    require(access.user_id == USER, "the link is minted for the turn's user")
    return access.key


async def test_an_html_file_from_a_slack_turn_arrives_as_the_users_link(monkeypatch):
    world = SeamWorld(monkeypatch)
    world.agent("router_agent").play(_answers(
        "Here is your page.",
        rich=RichContent("file", {"filename": "heat-pumps.html", "content": PAGE}, "the page heat-pumps.html")))
    chat = ChatChannel(*OTHER_SLACK)

    await world.chat_handler(rich_content_service=_slack_rich_content(world)).handle_message(
        slack_turn("Make me a page about heat pumps in Springfield."), chat)

    require(not world.unscripted(), "every agent call was scripted")
    [link] = _links_in(chat.texts)
    assert link.startswith(f"{SERVICE_URL}/f/")
    key = _key_of(world, link)
    assert key.startswith("html/") and key.endswith("-heat-pumps.html")
    assert b"Heat pumps in Springfield" in world.gcs.live(BUCKET, key).data
    assert world.media["slack"].uploads == [] and chat.rich == []
    assert not any("html/" in t and f"{SERVICE_URL}/f/" not in t for t in chat.texts)


async def test_an_html_gcs_link_item_from_a_slack_turn_arrives_as_the_users_link(monkeypatch):
    world = SeamWorld(monkeypatch)
    world.agent("router_agent").play(_answers("Found it.", items=[DeliveryItem(type="html_gcs_link", data={
        "html": PAGE, "filename": "sources.html", "link_text": "Sources"})]))
    chat = ChatChannel(*OTHER_SLACK)

    await world.chat_handler(rich_content_service=_slack_rich_content(world)).handle_message(
        slack_turn("What do heat pumps in Springfield cost?"), chat)

    require(not world.unscripted(), "every agent call was scripted")
    [link] = _links_in(chat.texts)
    assert link.startswith(f"<{SERVICE_URL}/f/") and link.endswith("|Sources>")
    key = _key_of(world, link)
    assert key.startswith("html/") and key.endswith("-sources.html")
    assert world.gcs.live(BUCKET, key) is not None
