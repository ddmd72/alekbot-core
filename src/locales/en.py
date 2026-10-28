"""
English UI messages library (playful tone).
Shared across all adapters (Slack, Telegram, Web, etc.)

Adapters can override specific messages if needed, but 99% will use these.
"""
from typing import Dict, List, Optional
from ..domain.ui_messages import StatusType, UIMessage


ENTERTAINMENT_INTROS: List[str] = [
    "Entertain yourself while I dig through the web",
    "While I search — hold an ironic pause",
    "Keep your brain busy while mine searches",
    "A fact break while I google",
    "Relax, it's a search, not a flight to Mars",
    "Hold this short story while I'm on my way",
    "While I rummage — here's your mini-dessert",
]

# Fallback prompts when user sends file without text
FILE_FALLBACK_IMAGE = "What's in this photo?"
FILE_FALLBACK_VIDEO = "What's in this video?"
FILE_FALLBACK_PDF = "Tell me about this document"
FILE_FALLBACK_DOCUMENT = "What's in this file?"
FILE_FALLBACK_GENERIC = "Take a look at this file"

# English messages (central library, playful tone)
EN_MESSAGES: Dict[str, List[str]] = {
    StatusType.THINKING.value: [
        "Contemplating the meaning of existence... and your question",
        "Syncing my neurons... please wait, this hurts",
        "Diving deep into my own mind",
        "Building logical chains... hoping they don't break",
        "Activating cognitive modules at full power",
        "Trying not to overheat from your brilliant ideas",
        "Consulting with my personality core",
        "Gathering my thoughts (they keep running away)",
        "Weighing the options... most are silly, hunting for the smart one",
        "Skimming my inner manual 'How to Sound Clever'",
        "Warming my circuits up to operating temperature",
        "Thinking so hard you can hear my fan spin up",
        "Consulting my subconscious... it's silent, as usual",
        "Switching off 'lazy mode', switching on 'genius mode'",
        "Summoning the muse... she's on a smoke break again",
        "Computing the answer across 47 parallel universes",
        "Dusting off my algorithms",
        "Meditating on your request like a proper monk",
        "Rebooting my common sense... almost there",
        "Looking for an answer worthy of your question",
        "Holding a pause for dramatic effect",
        "One more second... I'm almost a genius",
        "Running the scenarios where I definitely don't mess up"
    ],
    StatusType.SEARCHING_MEMORY.value: [
        "Flipping through your archives... it was here somewhere",
        "Searching your memory... hope it's clean in there",
        "Asking my inner librarian",
        "Diving into the ocean of your facts",
        "Pulling memories from the darkest corners",
        "Conducting inventory of your knowledge",
        "Looking for a needle in your memory haystack"
    ],
    StatusType.SEARCHING_WEB.value: [
        "Venturing into the wild internet... wish me luck",
        "Googling like my life depends on it",
        "Searching for answers in the world wide web",
        "Asking smart people on the net",
        "Scanning digital horizons",
        "Cutting through the jungle of information noise",
        "Hunting for fresh facts online"
    ],
    StatusType.PROCESSING_FILE.value: [
        "Analyzing your files",
        "Breaking down the document to atoms",
        "Studying your attachments",
    ],
    StatusType.ERROR.value: [
        "Oops! My neurons got tangled",
        "Something went wrong... probably Mercury retrograde",
        "An error occurred, but I'll get better (someday)",
        "My internal processor says 'ouch'",
        "Seems like I outsmarted myself",
        "System fell into existential crisis",
        "Error 404: My brain not found"
    ]
}


def get_message(status_type: StatusType, overrides: Optional[Dict[str, List[str]]] = None) -> List[str]:
    """
    Get English message for status type.
    
    Args:
        status_type: Type of status message
        overrides: Optional dict to override specific status types
        
    Returns:
        List of message variants
        
    Example:
        # Use default English
        messages = get_message(StatusType.THINKING)
        
        # Override specific type
        messages = get_message(
            StatusType.THINKING,
            overrides={StatusType.THINKING.value: ["Custom thinking..."]}
        )
    """
    if overrides and status_type.value in overrides:
        return overrides[status_type.value]
    
    return EN_MESSAGES.get(status_type.value, ["Processing..."])
# Fixed single-string UI messages (see domain.ui_messages.UIMessage)
UI_STRINGS: Dict[str, str] = {
    UIMessage.RESPONSE_READY.value: "✅ Response ready.",
    UIMessage.RESPONSE_TRUNCATED_SUFFIX.value: "\n\n... (response truncated)",
    UIMessage.EMPTY_MODEL_RESPONSE.value: "*(empty response from the model)*",
    UIMessage.UNKNOWN_COMMAND.value: "Unknown command: `{command}`",
    UIMessage.NEW_TOPIC_ACK.value: "New topic. History cleared.",
    UIMessage.SOURCES_HEADING.value: "Sources:",
    UIMessage.CONSOLIDATION_STARTED.value: "🧠 Memory consolidation started…",
    UIMessage.VOICE_REQUEST_FAILED.value: "The request did not go through.",
    UIMessage.SKILL_USAGE.value: (
        "Skills: `$skill list` — your skills · `$skill save CODE` — save a draft · "
        "`$skill delete NAME` — delete one of your skills"
    ),
    UIMessage.SKILL_SAVED.value: "✅ Skill `{name}` saved (v{version}). It is available from your next message.",
    UIMessage.SKILL_DRAFT_NOT_FOUND.value: "❌ No draft with code `{code}`. Ask me to draft the skill again.",
    UIMessage.SKILL_NOT_SAVED.value: "❌ Not saved: {reason}",
    UIMessage.SKILL_LIST_HEADER.value: "Your skills:",
    UIMessage.SKILL_LIST_EMPTY.value: "You have no skills of your own yet.",
    UIMessage.SKILL_SYSTEM_HEADER.value: "Built-in:",
    UIMessage.SKILL_DELETED.value: "🗑️ Skill `{name}` deleted.",
    UIMessage.SKILL_NOT_FOUND.value: "❌ No skill named `{name}`.",
    UIMessage.SKILL_BUILT_IN.value: "`{name}` is built in and cannot be deleted.",
    UIMessage.SKILL_UNAVAILABLE.value: "Skills are not available right now.",
    UIMessage.SKILL_PREVIEW_FILE_TITLE.value: "Skill draft: {name}",
    UIMessage.SKILL_PREVIEW_DELIVERY_FAILED.value: "⚠️ The skill draft could not be delivered. Ask me to draft it again.",
    UIMessage.SKILL_PREVIEW_REF_FILE_TITLE.value: "Skill draft: {name} — {path}",
    UIMessage.SKILL_LIST_FILES.value: "files: {count}",
    UIMessage.SKILL_CHANGE_NEW.value: "+ {path} (new, {size})",
    UIMessage.SKILL_CHANGE_NEW_UPLOAD.value: '+ {path} ← "{source}" (your upload, {size})',
    UIMessage.SKILL_CHANGE_CHANGED.value: "~ {path} (changed)",
    UIMessage.SKILL_CHANGE_CHANGED_UPLOAD.value: '~ {path} ← "{source}" (changed)',
    UIMessage.SKILL_CHANGE_REMOVED.value: "− {path} (removed)",
    UIMessage.SKILL_CHANGE_UNCHANGED.value: "= unchanged files: {count}",
    UIMessage.MEMORY_PREVIEW_FILE_TITLE.value: "Facts for memory: {count}",
    UIMessage.MEMORY_PREVIEW_HEADER.value: "🧠 Facts for long-term memory ({count}) are in the file above — nothing is saved until you paste the command below.",
    UIMessage.MEMORY_PREVIEW_DELIVERY_FAILED.value: "⚠️ The list of facts could not be delivered. Ask me to propose them again.",
    UIMessage.MEMORY_USAGE.value: "Memory: `$memory save CODE` — save the facts I proposed",
    UIMessage.MEMORY_SAVED.value: "✅ Sent to memory: {count}. They become searchable after background consolidation, usually within hours.",
    UIMessage.MEMORY_DRAFT_NOT_FOUND.value: "❌ No proposal with code `{code}`. Ask me to propose the facts again.",
    UIMessage.MEMORY_UNAVAILABLE.value: "Saving facts is not available right now.",
    UIMessage.DRIVE_NOTES_USAGE.value: 'Drive notes: `$drive-notes erase-all` — erase all your drive notes and their history for good (I send a code to confirm); paste `$drive-notes erase CODE` or `$drive-notes erase-all CODE` to confirm',
    UIMessage.DRIVE_NOTES_UNAVAILABLE.value: 'Drive notes are not available right now.',
    UIMessage.DRIVE_NOTES_NONE.value: 'You have no drive notes.',
    UIMessage.DRIVE_NOTES_NO_SUCH_REQUEST.value: '❌ No such erase request: the code is wrong, expired or already used. Nothing was erased.',
    UIMessage.LONG_TURN_NOTICE.value: "⏳ Working on it — this one takes longer. I'll answer when done; you can keep writing.",
    UIMessage.LATE_ANSWER_PREFIX.value: "[late answer]",
    UIMessage.LONG_TURN_FAILED.value: "I couldn't finish this one — something broke along the way.",
    UIMessage.LONG_TURN_CANCELLED.value: "Stopped, as you asked.",
    UIMessage.LONG_TURN_LOST.value: "I lost this one mid-way (the service restarted). Please ask again.",
    UIMessage.DRIVE_DELETED_FILE.value: "🗑️ Deleted from the drive: {path}",
    UIMessage.DRIVE_DELETED_FOLDER.value: "🗑️ Deleted folder: {path} (files: {count})",
    UIMessage.DRIVE_REPLACED.value: "✏️ Replaced: {path} ({before} → {after}; the previous version is in its version history)",
    UIMessage.DRIVE_DELETE_FAILED.value: "⚠️ Deleting {path} did not complete.",
    UIMessage.DRIVE_REPLACE_FAILED.value: "⚠️ Replacing {path} did not complete.",
    UIMessage.DRIVE_MIRROR_WRITE_REFUSED.value: "This folder is managed by the bot and its files are overwritten; to keep your own version, copy the file to another folder.",
    UIMessage.DRIVE_TRANSCRIPTS_README.value: (
        "# Transcripts\n\n"
        "Managed by the bot: files here may be rewritten; do not edit them. "
        "The bot overwrites every file here, in Archive/ too, so a change you make does not last. "
        "To keep your own version, copy the file to another folder. "
        "Transcripts of deleted recordings are in Archive/; deleting a file there makes the bot forget it."
    ),
    UIMessage.DRIVE_TRANSCRIPT_OPEN_RECORDING.value: "open the recording",
    UIMessage.DRIVE_TRANSCRIPT_MANAGED_LINE.value: (
        "Managed by the bot: this file may be rewritten. Automatic transcript, may contain errors."
    ),
    UIMessage.DRIVE_TRANSCRIPT_DURATION_MINUTES.value: "{n} min",
    UIMessage.DRIVE_TRANSCRIPT_SPEAKERS_ONE.value: "{n} speaker",
    UIMessage.DRIVE_TRANSCRIPT_SPEAKERS_MANY.value: "{n} speakers",
    UIMessage.DRIVE_TRANSCRIPT_STUB_NO_SPEECH.value: "No speech was detected in this recording ({duration}).",
    UIMessage.DRIVE_TRANSCRIPT_STUB_TOO_LONG.value: "This recording is too long to transcribe ({duration}).",
    UIMessage.DRIVE_TRANSCRIPT_STUB_TOO_LARGE.value: "This recording is too large to transcribe.",
    UIMessage.DRIVE_TRANSCRIPT_STUB_NO_AUDIO.value: "This recording has no audio track.",
    UIMessage.DEEP_RESEARCH_DRIVE_FOLDER.value: "Deep research",
    UIMessage.DEEP_RESEARCH_DRIVE_COPIED.value: "📁 Copied to your drive: {path}",
    UIMessage.DEEP_RESEARCH_DRIVE_NOT_CONNECTED.value: "Not copied to your drive: the drive is not connected.",
    UIMessage.DEEP_RESEARCH_DRIVE_COPY_FAILED.value: "Not copied to your drive: saving failed.",
    UIMessage.DRIVE_INACCESSIBLE_NOTICE.value: (
        "⚠️ Files in your drive folder that the bot cannot open because of their sharing settings: {count}. "
        "To fix it, copy each file into a new folder you create inside the bot's folder; the copies can be read. "
        "The list is attached."
    ),
    UIMessage.DRIVE_INACCESSIBLE_LIST_TITLE.value: "Files the bot cannot open",
    UIMessage.MESSAGE_NOT_PROCESSED_BUSY.value: "⚠️ I could not process your last message because I was busy for too long. Please send it again.",
    UIMessage.MEMORY_REVIEW_QUESTION.value: "Is this still true?",
    UIMessage.MEMORY_OPTION_STILL_TRUE.value: "Yes",
    UIMessage.MEMORY_OPTION_ENDED.value: "No longer",
    UIMessage.MEMORY_OPTION_NEVER.value: "Never",
    UIMessage.MEMORY_OPTION_DONT_KNOW.value: "Don't remember",
    UIMessage.MEMORY_REPLY_HINT.value: "Reply to this message with a digit, or in your own words.",
    UIMessage.MEMORY_ANSWER_NOTED.value: "Thank you, recorded.",
    UIMessage.MEMORY_REPLY_NOTED.value: "Noted — I'll take it into account.",
    UIMessage.MEMORY_ALREADY_ANSWERED.value: "Already answered, thank you.",
    UIMessage.MEMORY_FACT_CHANGED.value: "That fact has changed in the meantime, so I didn't apply your answer; I'll come back to it.",
    UIMessage.MEMORY_QUESTION_CLOSED.value: "This question is closed.",
    UIMessage.MEMORY_DIGEST_HEADER.value: "Facts I put aside this week:",
    UIMessage.MEMORY_DIGEST_FOOTER.value: "To bring one back, tell me in chat that it is still true.",
    UIMessage.MEMORY_DIGEST_ARCHIVED.value: "no longer true",
    UIMessage.MEMORY_DIGEST_INVALIDATED.value: "never was true",
    UIMessage.MEMORY_REVIEW_QUESTION_HAPPENED.value: "Did this happen?",
    UIMessage.MEMORY_REVIEW_QUESTION_RENEWED.value: "Was this renewed?",
    UIMessage.MEMORY_REVIEW_QUESTION_PAID.value: "Was this paid?",
    UIMessage.MEMORY_OPTION_HAPPENED.value: "It happened",
    UIMessage.MEMORY_OPTION_NOT_HAPPENED.value: "It did not",
    UIMessage.MEMORY_OPTION_RENEWED.value: "Renewed",
    UIMessage.MEMORY_OPTION_NOT_RENEWED.value: "Not renewed",
    UIMessage.MEMORY_OPTION_NOT_NEEDED.value: "No longer needed",
    UIMessage.MEMORY_OPTION_PAID.value: "Paid",
    UIMessage.MEMORY_OPTION_NOT_PAID.value: "Not paid",
    UIMessage.MEMORY_DIGEST_HISTORY_HEADER.value: "Recorded as history without your answer:",
    UIMessage.MEMORY_DIGEST_INVOICES_HEADER.value: "Invoices with no payment confirmation:",
    UIMessage.MEMORY_DIGEST_INVOICES_CABINET.value: "Mark the paid ones in the Cabinet:",
    UIMessage.DRIVE_NOTE_SAVED.value: "📝 Note saved on {path} (version {version}):",
    UIMessage.DRIVE_NOTE_UNCHANGED.value: "📝 The note on {path} already says exactly this (version {version}); nothing changed:",
    UIMessage.DRIVE_NOTE_CLEARED.value: "🗑️ Note removed from {path}. Its history is kept, so undo is possible; say \"erase the history\" to remove it for good.",
    UIMessage.DRIVE_NOTE_NOTHING_TO_CLEAR.value: "There was no note on {path}; nothing changed.",
    UIMessage.DRIVE_NOTE_RESTORED.value: "↩️ Previous note put back on {path} (version {version}):",
    UIMessage.DRIVE_NOTE_REDONE.value: "↪️ The undone change is back on {path} (version {version}):",
    UIMessage.DRIVE_NOTE_RESTORED_EMPTY.value: "↩️ Undone on {path}: there was no note before, so it is removed (version {version}).",
    UIMessage.DRIVE_NOTE_UNDO_REPEATED.value: "↩️ Already undone on {path} (version {version}); nothing changed:",
    UIMessage.DRIVE_NOTE_TYPE.value: "type: {doc_type}",
    UIMessage.DRIVE_NOTE_AREAS.value: "life areas: {areas}",
    UIMessage.DRIVE_NOTE_UNDO_HINT.value: "To undo, just say so.",
    UIMessage.DRIVE_NOTE_STATUS_SOON.value: "🔎 Search shows it within a few minutes.",
    UIMessage.DRIVE_NOTE_STATUS_INDEXING_OFF.value: "🔎 Drive search is off, so search shows it only once you turn \"Search my drive\" on in the Cabinet.",
    UIMessage.DRIVE_NOTE_STATUS_NOT_INDEXED.value: "🔎 This file is not in the search index (it sits inside a folder the index keeps as one card, or was not seen yet), so search does not show the note.",
    UIMessage.DRIVE_NOTE_STATUS_NO_CARD.value: "🔎 This file has no search card right now, so search does not show the note yet.",
    UIMessage.DRIVE_NOTE_STATUS_ARCHIVED.value: "🔎 The file is gone from the drive: only a search of deleted files finds this note.",
    UIMessage.DRIVE_NOTE_STATUS_OTHER_DRIVE.value: "🔎 Its drive isn't connected, so search does not show this note until that drive is connected again.",
    UIMessage.DRIVE_NOTE_ERASE_REQUEST.value: "🗑️ To erase the note on {path} and its whole history (versions: {versions}) for good, paste the command below within {minutes} minutes. Nothing is erased until you do.",
    UIMessage.DRIVE_NOTE_ERASE_ALL_REQUEST.value: "🗑️ To erase all your drive notes (notes: {count}) and their history for good, paste the command below within {minutes} minutes. Nothing is erased until you do.",
    UIMessage.DRIVE_NOTE_ERASED.value: "🗑️ Notes erased for good, with their history: {count}.",
    UIMessage.DRIVE_NOTE_ERASE_PARTIAL.value: "🗑️ Notes erased for good: {erased}. Not erased right now: {left}; ask me to erase it again.",
    UIMessage.DRIVE_NOTE_ERASE_ALL_PARTIAL.value: "🗑️ Notes erased for good: {erased}. Not erased right now: {left}; send `$drive-notes erase-all` again to erase them.",
}
