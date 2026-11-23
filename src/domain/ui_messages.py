"""
Platform-agnostic status types and default messages.
"""
from enum import Enum


class StatusType(Enum):
    """Semantic status types for conversation processing."""
    THINKING = "thinking"
    SEARCHING_MEMORY = "search_memory"
    SEARCHING_WEB = "search_web"
    PROCESSING_FILE = "processing_file"
    ERROR = "error"


class UIMessage(Enum):
    """Fixed single-string UI messages resolved via LocalizationPort.

    Values are keys into each locale module's UI_STRINGS dict. Entries may be
    str.format templates (e.g. UNKNOWN_COMMAND uses ``{command}``).
    """
    RESPONSE_READY = "response_ready"
    RESPONSE_TRUNCATED_SUFFIX = "response_truncated_suffix"
    EMPTY_MODEL_RESPONSE = "empty_model_response"
    UNKNOWN_COMMAND = "unknown_command"
    NEW_TOPIC_ACK = "new_topic_ack"
    SOURCES_HEADING = "sources_heading"
    CONSOLIDATION_STARTED = "consolidation_started"
    # A request Lelik sent during a call failed after the call stopped waiting for it.
    VOICE_REQUEST_FAILED = "voice_request_failed"
    # $skill command family (save/list/delete a custom skill).
    SKILL_USAGE = "skill_usage"
    SKILL_SAVED = "skill_saved"
    SKILL_DRAFT_NOT_FOUND = "skill_draft_not_found"
    SKILL_NOT_SAVED = "skill_not_saved"
    SKILL_LIST_HEADER = "skill_list_header"
    SKILL_LIST_EMPTY = "skill_list_empty"
    SKILL_SYSTEM_HEADER = "skill_system_header"
    SKILL_DELETED = "skill_deleted"
    SKILL_NOT_FOUND = "skill_not_found"
    SKILL_BUILT_IN = "skill_built_in"
    SKILL_UNAVAILABLE = "skill_unavailable"
    # skill_preview delivery (ConversationHandler._deliver_item) — no MessageContext there,
    # language comes from the response_channel instead (see _ui_string_for_language).
    SKILL_PREVIEW_FILE_TITLE = "skill_preview_file_title"
    SKILL_PREVIEW_DELIVERY_FAILED = "skill_preview_delivery_failed"
    # skill_preview: model-written reference files + change summary (delivery C, Task 7).
    SKILL_PREVIEW_REF_FILE_TITLE = "skill_preview_ref_file_title"
    SKILL_LIST_FILES = "skill_list_files"
    SKILL_CHANGE_NEW = "skill_change_new"
    SKILL_CHANGE_NEW_UPLOAD = "skill_change_new_upload"
    SKILL_CHANGE_CHANGED = "skill_change_changed"
    SKILL_CHANGE_CHANGED_UPLOAD = "skill_change_changed_upload"
    SKILL_CHANGE_REMOVED = "skill_change_removed"
    SKILL_CHANGE_UNCHANGED = "skill_change_unchanged"
    # Memory facts the owner confirms (MEMORY_FACT_CONFIRMATION_RFC): preview + $memory command.
    MEMORY_PREVIEW_FILE_TITLE = "memory_preview_file_title"
    MEMORY_PREVIEW_HEADER = "memory_preview_header"
    MEMORY_PREVIEW_DELIVERY_FAILED = "memory_preview_delivery_failed"
    MEMORY_USAGE = "memory_usage"
    MEMORY_SAVED = "memory_saved"
    MEMORY_DRAFT_NOT_FOUND = "memory_draft_not_found"
    MEMORY_UNAVAILABLE = "memory_unavailable"
    # Owner notes on drive files (DRIVE_FILE_NOTES_RFC §4.8): the `$drive-notes` command's own replies (part C). The
    # request, erased and partial replies are part A's DRIVE_NOTE_ERASE_* strings, rendered by drive_note_receipt.
    DRIVE_NOTES_USAGE = "drive_notes_usage"
    DRIVE_NOTES_UNAVAILABLE = "drive_notes_unavailable"
    DRIVE_NOTES_NONE = "drive_notes_none"
    DRIVE_NOTES_NO_SUCH_REQUEST = "drive_notes_no_such_request"
    # Long-running turns (LONG_RUNNING_TURNS_RFC §3, §5.3–5.7).
    LONG_TURN_NOTICE = "long_turn_notice"
    LATE_ANSWER_PREFIX = "late_answer_prefix"
    LONG_TURN_FAILED = "long_turn_failed"
    LONG_TURN_CANCELLED = "long_turn_cancelled"
    LONG_TURN_LOST = "long_turn_lost"
    # User drive receipts (USER_DRIVE_RFC §4.10) — destructive actions only.
    DRIVE_DELETED_FILE = "drive_deleted_file"
    DRIVE_DELETED_FOLDER = "drive_deleted_folder"
    DRIVE_REPLACED = "drive_replaced"
    DRIVE_DELETE_FAILED = "drive_delete_failed"
    DRIVE_REPLACE_FAILED = "drive_replace_failed"
    DRIVE_MIRROR_WRITE_REFUSED = "drive_mirror_write_refused"   # the Step 1 guard (USER_DRIVE_INDEXING_RFC §27.9.8)
    # The transcript mirror's files on the user's drive (USER_DRIVE_INDEXING_RFC §27.9.1, §27.9.10; D49): the read-me
    # of `_Transcripts`, the file header's labels and managed line, and the four stub sentences. Folder names are
    # fixed in every language (D49: `_Transcripts`, `Archive`, `Long paths`), so none is localized here. Counts use
    # `{n}`, the stub sentences `{duration}`; all go through `str.format`.
    DRIVE_TRANSCRIPTS_README = "drive_transcripts_readme"
    DRIVE_TRANSCRIPT_OPEN_RECORDING = "drive_transcript_open_recording"
    DRIVE_TRANSCRIPT_MANAGED_LINE = "drive_transcript_managed_line"
    DRIVE_TRANSCRIPT_DURATION_MINUTES = "drive_transcript_duration_minutes"
    DRIVE_TRANSCRIPT_SPEAKERS_ONE = "drive_transcript_speakers_one"
    DRIVE_TRANSCRIPT_SPEAKERS_MANY = "drive_transcript_speakers_many"
    DRIVE_TRANSCRIPT_STUB_NO_SPEECH = "drive_transcript_stub_no_speech"
    DRIVE_TRANSCRIPT_STUB_TOO_LONG = "drive_transcript_stub_too_long"
    DRIVE_TRANSCRIPT_STUB_TOO_LARGE = "drive_transcript_stub_too_large"
    DRIVE_TRANSCRIPT_STUB_NO_AUDIO = "drive_transcript_stub_no_audio"
    # Deep research report copy to the user's drive (decisions/deep_research_drive_copy.md): the drive
    # folder's name per UI language, and the one line that follows the delivered report's link.
    DEEP_RESEARCH_DRIVE_FOLDER = "deep_research_drive_folder"
    DEEP_RESEARCH_DRIVE_COPIED = "deep_research_drive_copied"
    DEEP_RESEARCH_DRIVE_NOT_CONNECTED = "deep_research_drive_not_connected"
    DEEP_RESEARCH_DRIVE_COPY_FAILED = "deep_research_drive_copy_failed"
    # Files the bot cannot open (USER_DRIVE_INDEXING_RFC §26.2, D33): the fan-out's notice. The count and the fix
    # only; the paths travel in the attached list, so no file name is ever part of the chat text.
    DRIVE_INACCESSIBLE_NOTICE = "drive_inaccessible_notice"
    DRIVE_INACCESSIBLE_LIST_TITLE = "drive_inaccessible_list_title"
    # A reminder fire that ended final with nothing sent: not re-run (`execute_reminder`); `{name}` is the note text.
    REMINDER_NOT_RUN = "reminder_not_run"
    # Slack message dropped after the session stayed busy through every re-queue (s1b AB2).
    MESSAGE_NOT_PROCESSED_BUSY = "message_not_processed_busy"
    # The memory review's question and its replies (Facts S4 plan §3.17, P2-3). These twenty-eight are also the label set
    # of `fact_review_language`: a language without UI strings gets a checked model-written copy under the same keys.
    MEMORY_REVIEW_QUESTION = "memory_review_question"
    MEMORY_OPTION_STILL_TRUE = "memory_option_still_true"
    MEMORY_OPTION_ENDED = "memory_option_ended"
    MEMORY_OPTION_NEVER = "memory_option_never"
    MEMORY_OPTION_DONT_KNOW = "memory_option_dont_know"
    MEMORY_REPLY_HINT = "memory_reply_hint"
    MEMORY_ANSWER_NOTED = "memory_answer_noted"
    MEMORY_REPLY_NOTED = "memory_reply_noted"
    MEMORY_ALREADY_ANSWERED = "memory_already_answered"
    MEMORY_FACT_CHANGED = "memory_fact_changed"
    MEMORY_QUESTION_CLOSED = "memory_question_closed"
    MEMORY_DIGEST_HEADER = "memory_digest_header"
    MEMORY_DIGEST_FOOTER = "memory_digest_footer"
    MEMORY_DIGEST_ARCHIVED = "memory_digest_archived"
    MEMORY_DIGEST_INVALIDATED = "memory_digest_invalidated"
    MEMORY_REVIEW_QUESTION_HAPPENED = "memory_review_question_happened"
    MEMORY_REVIEW_QUESTION_RENEWED = "memory_review_question_renewed"
    MEMORY_REVIEW_QUESTION_PAID = "memory_review_question_paid"
    MEMORY_OPTION_HAPPENED = "memory_option_happened"
    MEMORY_OPTION_NOT_HAPPENED = "memory_option_not_happened"
    MEMORY_OPTION_RENEWED = "memory_option_renewed"
    MEMORY_OPTION_NOT_RENEWED = "memory_option_not_renewed"
    MEMORY_OPTION_NOT_NEEDED = "memory_option_not_needed"
    MEMORY_OPTION_PAID = "memory_option_paid"
    MEMORY_OPTION_NOT_PAID = "memory_option_not_paid"
    MEMORY_DIGEST_HISTORY_HEADER = "memory_digest_history_header"
    MEMORY_DIGEST_INVOICES_HEADER = "memory_digest_invoices_header"
    MEMORY_DIGEST_INVOICES_CABINET = "memory_digest_invoices_cabinet"
    # Profile questions (FACT_PROFILE_RFC §4.7): the question line of each kind, the two options only they have, and the
    # note that a reply in words could not be read (the form that is read, by kind), and the date question's own reply hint.
    MEMORY_PROFILE_PLACE_QUESTION = "memory_profile_place_question"
    MEMORY_PROFILE_PERSON_QUESTION = "memory_profile_person_question"
    MEMORY_PROFILE_DATE_QUESTION = "memory_profile_date_question"
    MEMORY_OPTION_KEEP_AS_WRITTEN = "memory_option_keep_as_written"
    MEMORY_OPTION_NOT_LISTED = "memory_option_not_listed"
    MEMORY_PROFILE_NOT_READ = "memory_profile_not_read"
    MEMORY_PROFILE_DATE_NOT_READ = "memory_profile_date_not_read"
    MEMORY_PROFILE_DATE_REPLY_HINT = "memory_profile_date_reply_hint"
    # Owner notes on drive files (DRIVE_FILE_NOTES_RFC §4.6, §4.8): the receipt's header per outcome, its classifier,
    # searchability and undo lines, and the erase request and outcome. Rendered by `domain.drive_note_receipt` only;
    # placeholders `{path}`, `{version}`, `{doc_type}`, `{areas}`, `{versions}`, `{count}`, `{minutes}`, `{erased}`,
    # `{left}`. The note's own text is never a template: the renderer appends it byte for byte.
    DRIVE_NOTE_SAVED = "drive_note_saved"
    DRIVE_NOTE_UNCHANGED = "drive_note_unchanged"
    DRIVE_NOTE_CLEARED = "drive_note_cleared"
    DRIVE_NOTE_NOTHING_TO_CLEAR = "drive_note_nothing_to_clear"
    DRIVE_NOTE_RESTORED = "drive_note_restored"
    DRIVE_NOTE_REDONE = "drive_note_redone"
    DRIVE_NOTE_RESTORED_EMPTY = "drive_note_restored_empty"
    DRIVE_NOTE_UNDO_REPEATED = "drive_note_undo_repeated"
    DRIVE_NOTE_TYPE = "drive_note_type"
    DRIVE_NOTE_AREAS = "drive_note_areas"
    DRIVE_NOTE_UNDO_HINT = "drive_note_undo_hint"
    DRIVE_NOTE_STATUS_SOON = "drive_note_status_soon"
    DRIVE_NOTE_STATUS_INDEXING_OFF = "drive_note_status_indexing_off"
    DRIVE_NOTE_STATUS_NOT_INDEXED = "drive_note_status_not_indexed"
    DRIVE_NOTE_STATUS_NO_CARD = "drive_note_status_no_card"
    DRIVE_NOTE_STATUS_ARCHIVED = "drive_note_status_archived"
    DRIVE_NOTE_STATUS_OTHER_DRIVE = "drive_note_status_other_drive"
    DRIVE_NOTE_ON_DRIVE = "drive_note_on_drive"
    DRIVE_NOTE_ERASE_REQUEST = "drive_note_erase_request"
    DRIVE_NOTE_ERASE_ALL_REQUEST = "drive_note_erase_all_request"
    DRIVE_NOTE_ERASED = "drive_note_erased"
    DRIVE_NOTE_ERASE_PARTIAL = "drive_note_erase_partial"
    DRIVE_NOTE_ERASE_ALL_PARTIAL = "drive_note_erase_all_partial"
    # Name of the drive Inbox subfolder that holds saved voice-call text files (VOICE_CALL_LIVE_TEXT_RFC §4.11).
    CALL_FILES_DRIVE_FOLDER = "call_files_drive_folder"
    # Geo marks (GEO_MARKS_RFC §4.4–4.6): the `$geo` link line (`{minutes}`) and its label, the confirmation the bot
    # posts and keeps as the model message of the pair (`{address}`, `{accuracy}`, `{time}`), the refusals, and the
    # Slack page's strings (`GeoMarkService.page_strings`; the page fills `{address}` and `{accuracy}` itself).
    GEO_LINK_LINE = "geo_link_line"
    GEO_LINK_LABEL = "geo_link_label"
    GEO_SAVED = "geo_saved"
    GEO_SAVED_WITH_LANDMARKS = "geo_saved_with_landmarks"
    GEO_SAVED_NO_ADDRESS = "geo_saved_no_address"
    GEO_NOT_SAVED = "geo_not_saved"
    GEO_UNAVAILABLE = "geo_unavailable"
    GEO_BOUND_CHANNEL = "geo_bound_channel"
    GEO_DM_ONLY = "geo_dm_only"
    GEO_USE_TELEGRAM_PIN = "geo_use_telegram_pin"
    GEO_TELEGRAM_VENUE = "geo_telegram_venue"
    GEO_TELEGRAM_LIVE = "geo_telegram_live"
    GEO_PAGE_LOCATING = "geo_page_locating"
    GEO_PAGE_SAVED = "geo_page_saved"
    GEO_PAGE_SAVED_NO_ADDRESS = "geo_page_saved_no_address"
    GEO_PAGE_QUESTION_SENT = "geo_page_question_sent"
    GEO_PAGE_QUESTION_NOT_SENT = "geo_page_question_not_sent"
    GEO_PAGE_DENIED = "geo_page_denied"
    GEO_PAGE_DENIED_HOWTO = "geo_page_denied_howto"
    GEO_PAGE_UNAVAILABLE = "geo_page_unavailable"
    GEO_PAGE_RETRY = "geo_page_retry"
    GEO_PAGE_GONE = "geo_page_gone"
    GEO_PAGE_NOT_SAVED = "geo_page_not_saved"
    # A new Cabinet device session started (CABINET_DEVICE_SESSIONS_RFC §7.5), signed in with Google or with a passkey;
    # {platform_hint} is untrusted text.
    DEVICE_SIGN_IN_NOTICE = "device_sign_in_notice"
    DEVICE_PASSKEY_SIGN_IN_NOTICE = "device_passkey_sign_in_notice"
    # `$devices` and `$devices off` (CABINET_DEVICE_SESSIONS_RFC §7.5, §7.6). {label} is untrusted text.
    DEVICES_UNAVAILABLE = "devices_unavailable"
    DEVICES_USAGE = "devices_usage"
    DEVICES_LIST_HEADER = "devices_list_header"
    DEVICES_LIST_EMPTY = "devices_list_empty"
    DEVICES_LIST_LINE = "devices_list_line"
    DEVICES_OFF_DONE = "devices_off_done"
    DEVICES_CHECKLIST = "devices_checklist"
    # A passkey added to or removed from the user's Cabinet sign-in (CABINET_DEVICE_SESSIONS_RFC §7.3, §7.5); {label} is
    # untrusted text.
    PASSKEY_ADDED_NOTICE = "passkey_added_notice"
    PASSKEY_REMOVED_NOTICE = "passkey_removed_notice"
    # The facts PIN (CABINET_DEVICE_SESSIONS_RFC §7.7): removed; a reset asked for ({platform_hint} is untrusted text,
    # {time} is when it takes effect) and applied.
    FACTS_PIN_REMOVED_NOTICE = "facts_pin_removed_notice"
    FACTS_PIN_RESET_REQUESTED_NOTICE = "facts_pin_reset_requested_notice"
    FACTS_PIN_RESET_DONE_NOTICE = "facts_pin_reset_done_notice"
    # The account lock (§7.8), posted to every stored channel: by the duress code, or by five wrong PINs in a row.
    ACCOUNT_LOCKED_DURESS_NOTICE = "account_locked_duress_notice"
    ACCOUNT_LOCKED_PIN_ATTEMPTS_NOTICE = "account_locked_pin_attempts_notice"
    # A `$<digits>` code in chat (§7.9): never echoing the code. No PIN set; the real PIN; a wrong code ({left}: the
    # attempts left); an account already locked; the check unavailable.
    FACTS_PIN_CHAT_NO_PIN = "facts_pin_chat_no_pin"
    FACTS_PIN_CHAT_REAL_PIN = "facts_pin_chat_real_pin"
    FACTS_PIN_CHAT_WRONG = "facts_pin_chat_wrong"
    FACTS_PIN_CHAT_LOCKED = "facts_pin_chat_locked"
    FACTS_PIN_CHAT_UNAVAILABLE = "facts_pin_chat_unavailable"
    # `$pin cancel` (§7.7): a pending reset cancelled; none pending; any other `$pin`.
    FACTS_PIN_RESET_CANCELLED = "facts_pin_reset_cancelled"
    FACTS_PIN_NO_RESET_PENDING = "facts_pin_no_reset_pending"
    FACTS_PIN_USAGE = "facts_pin_usage"
