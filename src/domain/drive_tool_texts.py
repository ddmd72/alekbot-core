"""The drive specialist's tools: names, descriptions written for the specialist, and their declarations
(DRIVE_SPECIALIST_RFC §8.1).

CRUD-shaped, one operation each. Flat schemas, no `enum` / `pattern` / `maxItems`: limits are stated in the
descriptions and enforced in code; `""` and an omitted field both mean "not given" and never remove or change
anything; no parameter is named `name`. `folder` takes a folder path or a `drive:` folder ref in every tool.
Today's search-field texts are re-voiced for the specialist (§7.6): field names become parameter names, the old
search intent's name prefix and the "query is not read" clause go.
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence

SEARCH_DRIVE = "search_drive"
SEARCH_DELETED = "search_deleted_recordings_and_notes"
LIST_FOLDER = "list_folder"
OPEN_DRIVE_FILE = "open_drive_file"
GET_RECORDING_LINK = "get_recording_link"
COPY_FILE_TO_DRIVE = "copy_file_to_drive"
SAVE_TEXT_FILE = "save_text_file"
CREATE_FOLDER = "create_folder"
MOVE_FILE = "move_file"
APPEND_TO_FILE = "append_to_file"
REPLACE_FILE_CONTENT = "replace_file_content"
DELETE_DRIVE_ITEM = "delete_drive_item"
REPORT_TOOL = "report_to_orchestrator"
# USER_DRIVE_INDEXING_RFC §28.3 A3: the passages tools; their declarations come with the tools.
FIND_PASSAGES = "find_passages"
READ_PASSAGES = "read_passages"

_FOLDER = "a folder path such as 'Contracts/2026', or a folder's drive: ref"

QUERY = (
    "REQUIRED unless the request is filter-only. The subject in the user's words (any language): the words of a file "
    "or folder name, a place, a person, a topic, a document type or purpose ('tickets', 'receipts', 'contracts', "
    "'what expires'), or what to find inside (a phrase, a number, an amount, an id). For 'everything about X' put X. "
    "A file format goes in kind ('photos', 'PDF', 'spreadsheet'); a time span ('last year', '2023–2024') goes in "
    "date_from / date_to, not in the words. Leave EMPTY only for filter-only requests. All words must match: use the "
    "user's few key words in one language."
)
SEARCH_FOLDER = f"Optional: search within this folder and its subfolders: {_FOLDER}. Empty searches the whole drive."
KIND = (
    "Optional file type. A family covers every format of its kind: spreadsheet (xlsx, xls, ods, csv), document "
    "(docx, doc, odt, rtf, pdf, markdown, text), word (docx, doc), presentation (pptx, ppt, odp), photo (any image). "
    "Or one kind: pdf, docx, xlsx, markdown, text, csv, image, video, audio, archive, code, folder, or a file "
    "extension such as 'jpg'. Other words are refused."
)
DATE_FROM = (
    "Optional earliest date, YYYY-MM-DD, inclusive, in the user's timezone: a photo's taken date, otherwise the date "
    "written in the document when the index found one, otherwise the file's date. Use it for a time span the user "
    "gives. For what expires, it bounds the expiry date."
)
DATE_TO = (
    "Optional latest date, YYYY-MM-DD, inclusive (the whole day counts), in the user's timezone: a photo's taken "
    "date, otherwise the date written in the document when the index found one, otherwise the file's date. Use it "
    "for a time span the user gives. For what expires, it bounds the expiry date."
)
DELETED_QUERY = "Optional: words to find. Empty lists the newest."
LIST_FOLDER_PARAM = f"Optional: {_FOLDER}. Empty lists the top level."
FILE_REF = ("The drive: ref of the file or folder, from a card, a [Drive: …] label, the commission or the state "
            "block.")
OPEN_REF = ("The file's drive: ref, from a card, a [Drive: …] label, the commission or the state block. A chat file "
            "name from a [File: …] label, a delivered document key, a workspace:<path> file or a skill: file is "
            "opened from storage.")
RECORDING_REF = "The recording's drive: ref."
SOURCE_REF = ("The file to keep: a chat file name from a [File: …] label, a delivered document key, a workspace:<path> "
              "file or a skill: file. Never a drive: ref (a file already on the drive is moved, not copied).")
WRITE_FOLDER = (f"Optional, only when the user named a folder: {_FOLDER}; created if missing. Empty saves to "
                "Inbox.")
COPY_FILE_NAME = "Optional: the file name to use, extension included; empty keeps the source's name and extension."
TEXT_ITEM = ("The file_contents item to write: t1, t2 … as the state block lists them. A value that names no item is "
             "refused and nothing is written.")
SAVE_FILE_NAME = ("Optional: the file name, ending in .md .txt .csv .json .yaml .yml .html .svg or .xml; empty uses "
                  "the item's name. Up to 5 MiB of text.")
CREATE_FOLDER_PARAM = f"The folder to create: {_FOLDER}. Missing parents are created."
MOVE_FOLDER = f"Optional: the destination folder: {_FOLDER}; created if missing."
NEW_NAME = "Optional: the new name, extension included for files. Give folder, new_name or both."
APPEND_REF = "Optional: the drive: ref of the text file; empty uses the item's target."
TARGET_REF = "The drive: ref of the file whose whole content is replaced."
NEW_CONTENT_REF = ("The file whose content goes in: a chat file name from a [File: …] label, a delivered document key, "
                   "a workspace:<path> file, a skill: file, or another drive: file.")
# USER_DRIVE_INDEXING_RFC §28.3 A3, A5, A7: the passages tools' fields.
PASSAGES_QUESTION = (
    "The full question the passages should answer, self-contained and in the language of the commission: what is "
    "asked about which file or subject ('What does Tom's plan say happens after the first review?'), not key words. "
    "You may reword it for a second try (other words, the documents' language)."
)
PASSAGES_FOLDER = (f"Optional: only files in this folder and its subfolders: {_FOLDER}. Empty searches the whole "
                   "drive.")
PASSAGES_FILE_REFS = ("Optional: only these files: their drive: refs, comma-separated, at most 10, from a card, a "
                      "passages head line, the commission or the state block. When given, folder is not used. Empty: "
                      "not limited to files.")
PASSAGES_FILE_REF = "The file's drive: ref, from a card or a passages head line's id."
PASSAGES_RANGE = ("The passage numbers to read: one number or a range such as 6-8, numbered as head lines number them "
                  "('passages i–j of N'); at most 10. The passages after a run i–j start at j+1; a cut result says "
                  "where it continues.")
NOTE = ("At most two sentences on what you searched, opened or changed. Never a file's content, never whether the files "
        "answer the question, never that something is complete or missing.")
QUESTION = ("Optional: only when the orchestrator must ask the user which file is meant; name the candidates by path. "
            "Empty otherwise.")
NOT_DONE = ("Optional: what was asked that no tool can do (for example restoring a deleted file, rewriting a file with "
            "new text). Empty otherwise.")


def _param(description: str) -> Dict[str, str]:
    return {"type": "string", "description": description}


def _tool(name: str, description: str, required: List[str], **params: str) -> Dict[str, Any]:
    schema: Dict[str, Any] = {"type": "object", "properties": {k: _param(v) for k, v in params.items()}}
    if required:
        schema["required"] = required
    return {"name": name, "description": description, "parameters": schema}


def drive_tool_declarations(extra: Sequence[Dict[str, Any]] = ()) -> List[Dict[str, Any]]:
    """The fourteen drive tools, then `extra` (the note tools, in their own fixed order), then the terminal tool: a fixed
    order, so each cacheable prefix stays stable (design Δ16). No `extra` is today's list byte for byte."""
    return [
        _tool(SEARCH_DRIVE, "Find files and folders on the drive by what they are, their names, dates, places and what "
              "is written in them.", [], query=QUERY, folder=SEARCH_FOLDER, kind=KIND, date_from=DATE_FROM,
              date_to=DATE_TO),
        _tool(SEARCH_DELETED, "Search the transcripts of deleted recordings and the notes on deleted files; deleted "
              "files themselves are not searchable. Only when the user explicitly asks about deleted or removed "
              "files. Never for an ordinary search.", [], query=DELETED_QUERY),
        _tool(LIST_FOLDER, "List one folder, folders first.", [], folder=LIST_FOLDER_PARAM),
        _tool(OPEN_DRIVE_FILE, "Open a drive file for the orchestrator: its text, the image or PDF itself, or a "
              "recording's transcript; a video is sent to the user's chat instead. You do not see what is opened.",
              ["file_ref"], file_ref=OPEN_REF),
        _tool(FIND_PASSAGES, "Read what the files say about a question: the indexed passages nearest it, verbatim "
              "excerpts numbered per file, shown to you in full and delivered to the orchestrator. For what is "
              "written inside files; to find which files there are use search_drive, for a whole file "
              "open_drive_file.", ["question"], question=PASSAGES_QUESTION, folder=PASSAGES_FOLDER,
              file_refs=PASSAGES_FILE_REFS),
        _tool(READ_PASSAGES, "Read one file's passages by number: the ones around or after a passage you were shown, "
              "or where a cut result continues. Shown to you in full and delivered to the orchestrator.",
              ["file_ref", "passages"], file_ref=PASSAGES_FILE_REF, passages=PASSAGES_RANGE),
        _tool(GET_RECORDING_LINK, "A link to a recording itself, only when the user asks for the recording, not its "
              "content.", ["file_ref"], file_ref=RECORDING_REF),
        _tool(COPY_FILE_TO_DRIVE, "Keep a copy of a file on the drive; never overwrites, the result states the name "
              "used.", ["source_ref"], source_ref=SOURCE_REF, folder=WRITE_FOLDER, file_name=COPY_FILE_NAME),
        _tool(SAVE_TEXT_FILE, "Save one attached text as a new text file; never overwrites.", ["text_item"],
              text_item=TEXT_ITEM, file_name=SAVE_FILE_NAME, folder=WRITE_FOLDER),
        _tool(CREATE_FOLDER, "Create a folder and any missing parents.", ["folder"], folder=CREATE_FOLDER_PARAM),
        _tool(MOVE_FILE, "Move and/or rename a file or folder in one step.", ["file_ref"], file_ref=FILE_REF,
              folder=MOVE_FOLDER, new_name=NEW_NAME),
        _tool(APPEND_TO_FILE, "Add one attached text at the end of a text file.", ["text_item"], text_item=TEXT_ITEM,
              file_ref=APPEND_REF),
        _tool(REPLACE_FILE_CONTENT, "Replace a drive file's whole content with another file's; the user gets a notice "
              "with old and new size.", ["target_ref", "new_content_ref"], target_ref=TARGET_REF,
              new_content_ref=NEW_CONTENT_REF),
        _tool(DELETE_DRIVE_ITEM, "Delete a file or a whole folder (recoverable from the recycle bin); the user gets a "
              "notice.", ["file_ref"], file_ref=FILE_REF),
        *extra,
        _tool(REPORT_TOOL, "Finish: what you did, in one or two sentences. The system delivers every result to the "
              "orchestrator.", [], note=NOTE, question=QUESTION, not_done=NOT_DONE),
    ]
