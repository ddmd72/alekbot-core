"""The request shapes the Microsoft Graph adapters send (To Do, OneDrive and the OAuth edge's `GET /me`), for the
request-conformance test (P17).

A shape is one call of a public adapter method with invented inputs. `test_graph_request_conformance.py` runs it on the
REAL adapter (`MicrosoftToDoAdapter` or `OneDriveAdapter`) over `tests/support/http_boundary_fake.py` (the test routes each
Graph URL with the smallest answer that lets the method run to its end) and checks every Graph request that leaves
against `contracts/providers/graph/openapi.yaml`, through its aliases. What the requests must look like is the contract,
not something written here.

Every kept operation, top-level body field, query parameter, `Allowed` gap and alias rule of the graph entry in
`scripts/contracts/extract_subset.py` is reached by a shape. `NOT_COVERED` names the URLs the adapters send that the
contract does not hold, each with its reason; `INDIRECT` names a URL expression that stands for a module constant.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from src.composition.connector_registry import ConnectorRegistry
from src.domain.flow_intent import FlowIntent
from src.domain.oauth_flow import OAuthFlow
from src.domain.task import (
    ChecklistItem,
    LinkedResource,
    RecurrencePattern,
    RecurrenceRange,
    TaskCreate,
    TaskImportance,
    TaskRecurrence,
    TaskStatus,
    TaskUpdate,
)

USER = "user-1"
LIST_ID = "list-1"
TASK_ID = "task-1"
SUB_ID = "sub-1"
ROOT_ID = "item-1"           # the App Folder
FILE_ID = "item-2"
FOLDER_ID = "item-3"
DRIVE_ID = "drive-1"
ROOT_NAME = "R&D"
FILE_NAME = "Tom's plan.docx"
SLASHED_NAME = "R&D/Tom's plan.docx"   # one name: the adapter encodes its slash (%2F), so it stays one segment
RENAMED = "Tom's plan v2.docx"
PDF_NAME = "birth-certificate.pdf"
SEARCH_TEXT = "Tom's plan"   # its quote is doubled inside the search function's literal
NOTIFY_URL = "https://alek.example.invalid/graph/notify"
CLIENT_STATE = "state-1"
LISBON = ZoneInfo("Europe/Lisbon")
SMALL = b"%PDF-1.7 Invoice FA-0137"
LARGE = b"%PDF" + b"0" * (4 * 1024 * 1024)   # one byte over the simple-upload limit: an upload session
REDIRECT_URI = "https://alek.example.invalid/oauth/callback"
FLOW = OAuthFlow(user_id=USER, kind="microsoft_onedrive", intent=FlowIntent.CONNECT, target_id="", expected_subject="",
                 hint_label="", pkce_verifier="verifier-1", return_path="/", expires_at=datetime(2026, 11, 19, 12, 0,
                                                                                                 tzinfo=timezone.utc))

# "<adapter file stem> <METHOD> <URL expression as the adapter writes it>" -> why the request has no operation in the
# Graph contract. The meta-test fails when an entry is covered by an operation now, or no longer sent.
NOT_COVERED: dict[str, str] = {
    "todo_adapter GET data.get('@odata.nextLink')": (
        "the next page of a task listing: an absolute URL Graph produced ($skiptoken is Graph's, not ours)"),
    "onedrive_adapter GET page.get('@odata.nextLink')": (
        "the next page of a folder listing: an absolute URL Graph produced ($skiptoken is Graph's, not ours)"),
    "onedrive_adapter GET cursor": (
        "a stored listing cursor, a nextLink Graph produced; a cursor outside Graph is never followed"),
    "onedrive_adapter GET raw.get('@microsoft.graph.downloadUrl')": (
        "the item's pre-authenticated download URL on a content host, not the Graph API"),
    "onedrive_adapter GET str(rendition.get('url') or '') if isinstance(rendition, dict) else ''": (
        "a thumbnail rendition's pre-authenticated URL on a content host, not the Graph API"),
    "onedrive_adapter PUT (await self._call(s, handle, 'POST', path, json=body))['uploadUrl']": (
        "an upload session's pre-authenticated uploadUrl, not the Graph API (no bearer token is sent there)"),
    "microsoft_oauth_provider POST https://login.microsoftonline.com/consumers/oauth2/v2.0/token": (
        "the identity platform's token endpoint, not the Graph API: its contract comes with Microsoft's identity "
        "platform (RFC P17 'Still open')"),
}

# "<adapter file stem> <METHOD> <URL expression>" -> the module constant it stands for, besides Graph's own continuations:
# `_decode_cursor(None)` is the delta start; a stored cursor is a deltaLink or nextLink Graph produced (not ours, as
# the NOT_COVERED continuations above).
INDIRECT: dict[str, str] = {
    "onedrive_adapter GET self._decode_cursor(cursor)": "_DELTA_START",
}

Call = Callable[[Any, Any], Awaitable[Any]]   # (adapter, drive handle or None) -> result


@dataclass(frozen=True)
class Shape:
    id: str
    adapter: str   # "todo" | "drive" | "oauth"
    call: Call


_DUE = datetime(2026, 11, 4, 18, 0, tzinfo=LISBON)
_START = datetime(2026, 11, 3, 8, 0, tzinfo=LISBON)
_REMINDER = datetime(2026, 11, 3, 9, 0, tzinfo=LISBON)
_RECURRENCE = TaskRecurrence(
    pattern=RecurrencePattern(type="relativeYearly", interval=1, day_of_month=4, days_of_week=["wednesday"],
                              first_day_of_week="monday", month=11, index="first"),
    range=RecurrenceRange(type="numbered", start_date="2026-11-04", end_date="2027-11-04", number_of_occurrences=2,
                          recurrence_time_zone="Europe/Lisbon"),
)
_CREATE = TaskCreate(
    title="car service Nov 4, bring the logbook", list_id=LIST_ID, body="Invoice FA-0137", due_datetime=_DUE,
    start_datetime=_START, reminder_datetime=_REMINDER, is_reminder_on=True, importance=TaskImportance.HIGH,
    tags=["Errands"], recurrence=_RECURRENCE,
    checklist_items=[ChecklistItem(item_id="", title="bring the logbook")],
    linked_resources=[LinkedResource(resource_id="", web_url="https://example.com/tasks/1",
                                     display_name="Invoice FA-0137", application_name="Example", external_id="task-1")],
)
_UPDATE = TaskUpdate(
    title="a library card renewal due Nov 30", body="Invoice FA-0137", due_datetime=_DUE, start_datetime=_START,
    reminder_datetime=_REMINDER, is_reminder_on=False, importance=TaskImportance.LOW, status=TaskStatus.IN_PROGRESS,
    tags=["Errands"], recurrence=_RECURRENCE,
)
# The routed task holds checklist items "item-1" and "item-2": keep item-1 renamed and checked, drop item-2, add one.
CHECKLIST = [ChecklistItem(item_id="item-1", title="renew the library card", is_completed=True),
             ChecklistItem(item_id="", title="bring the logbook")]


SHAPES: tuple[Shape, ...] = (
    # -------------------------------------------------------------------------------------------------- To Do
    Shape("todo/list_task_lists", "todo", lambda a, h: a.list_task_lists(USER)),
    Shape("todo/list_tasks/not_completed", "todo", lambda a, h: a.list_tasks(USER, LIST_ID)),
    Shape("todo/list_tasks/all", "todo", lambda a, h: a.list_tasks(USER, LIST_ID, show_completed=True)),
    Shape("todo/get_task", "todo", lambda a, h: a.get_task(USER, LIST_ID, TASK_ID)),
    Shape("todo/create_task/every_field", "todo", lambda a, h: a.create_task(USER, _CREATE)),
    Shape("todo/update_task/every_field", "todo", lambda a, h: a.update_task(USER, LIST_ID, TASK_ID, _UPDATE)),
    Shape("todo/update_task/checklist_sync", "todo",
          lambda a, h: a.update_task(USER, LIST_ID, TASK_ID, TaskUpdate(checklist_items=CHECKLIST))),
    Shape("todo/delete_task", "todo", lambda a, h: a.delete_task(USER, LIST_ID, TASK_ID)),
    Shape("todo/ensure_primary_list/create", "todo", lambda a, h: a.ensure_primary_list(USER)),
    Shape("todo/register_subscription", "todo", lambda a, h: a.register_subscription(USER, LIST_ID, NOTIFY_URL)),
    Shape("todo/renew_subscription", "todo", lambda a, h: a.renew_subscription(USER, SUB_ID)),
    Shape("todo/delete_subscription", "todo", lambda a, h: a.delete_subscription(USER, SUB_ID)),
    # ------------------------------------------------------------------------------------------------ OneDrive
    Shape("drive/read_drive_id", "drive", lambda a, h: a.read_drive_id("x")),
    Shape("drive/get_root", "drive", lambda a, h: a.get_root(h)),
    Shape("drive/area_path", "drive", lambda a, h: a.area_path(h)),
    Shape("drive/get_changes/start", "drive", lambda a, h: a.get_changes(h, None)),
    Shape("drive/get_item", "drive", lambda a, h: a.get_item(h, FILE_ID)),
    Shape("drive/get_change", "drive", lambda a, h: a.get_change(h, FILE_ID)),
    Shape("drive/media_source", "drive", lambda a, h: a.media_source(h, FILE_ID)),
    Shape("drive/download", "drive", lambda a, h: a.download(h, FILE_ID)),
    Shape("drive/list_children", "drive", lambda a, h: a.list_children(h, ROOT_ID)),
    Shape("drive/list_children_changes", "drive", lambda a, h: a.list_children_changes(h, ROOT_ID, 50)),
    Shape("drive/search", "drive", lambda a, h: a.search(h, SEARCH_TEXT, 5)),
    Shape("drive/child_by_name", "drive", lambda a, h: a.child_by_name(h, ROOT_ID, FILE_NAME)),
    Shape("drive/child_by_name/slash_in_the_name", "drive", lambda a, h: a.child_by_name(h, ROOT_ID, SLASHED_NAME)),
    Shape("drive/get_thumbnail", "drive", lambda a, h: a.get_thumbnail(h, FILE_ID, 300)),
    Shape("drive/upload/small_by_name", "drive",
          lambda a, h: a.upload(h, ROOT_ID, PDF_NAME, SMALL, "application/pdf")),
    Shape("drive/replace_content/small_by_id", "drive",
          lambda a, h: a.replace_content(h, FILE_ID, SMALL, "application/pdf")),
    Shape("drive/upload/session_by_name", "drive",
          lambda a, h: a.upload(h, ROOT_ID, PDF_NAME, LARGE, "application/pdf", conflict="replace")),
    Shape("drive/replace_content/session_by_id", "drive",
          lambda a, h: a.replace_content(h, FILE_ID, LARGE, "application/pdf")),
    Shape("drive/move_and_rename", "drive", lambda a, h: a.move(h, FILE_ID, new_parent_id=FOLDER_ID, new_name=RENAMED)),
    Shape("drive/create_folder", "drive", lambda a, h: a.create_folder(h, ROOT_ID, ROOT_NAME)),
    Shape("drive/delete", "drive", lambda a, h: a.delete(h, FILE_ID)),
    # ------------------------------------------------------------------------------------- the OAuth edge
    Shape("oauth/exchange", "oauth", lambda a, h: a.exchange(
        "code-1", FLOW, ConnectorRegistry().get("microsoft_onedrive"), REDIRECT_URI)),
)
