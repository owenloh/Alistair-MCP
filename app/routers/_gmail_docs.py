"""Descriptions for the Gmail connector tools (read + draft only).

Kept here to keep the router readable. Written in the same plain, behaviour-first
style as the other connector descriptions so a voice-mode Claude calls them correctly.
"""

SEARCH = (
    "Search the user's Gmail using Gmail query syntax (e.g. 'from:bank newer_than:7d', "
    "'subject:invoice has:attachment', 'is:unread label:work'). Read-only. Returns "
    "lightweight message stubs (from, to, subject, date, snippet, id, thread_id) — "
    "follow up with get-thread using the thread_id to read the full conversation. "
    "Keep max_results low (default 20, max 25) to minimise response size."
)

GET_THREAD = (
    "Read one Gmail thread by its thread_id, with every message rendered to clean text "
    "(From/To/Subject/Date headers + decoded body, preferring text/plain and stripping "
    "HTML). Read-only. Use after search to read a conversation in full; summarise it for "
    "the user rather than dumping the raw text."
)

LIST_DRAFTS = (
    "List the user's existing Gmail drafts (draft_id, to, subject, snippet, thread_id). "
    "Read-only. Use to find a draft to update or delete, or to show what is queued."
)

CREATE_DRAFT = (
    "Create a Gmail DRAFT. This NEVER sends — it only saves a draft to the user's Drafts "
    "for them to review and send. Provide to, subject and body; optional cc/bcc. To draft a "
    "reply that threads correctly, pass thread_id (and in_reply_to set to the original "
    "message's Message-ID). Write in the user's voice, keep it tight, then show them the draft."
)

UPDATE_DRAFT = (
    "Replace the contents of an existing Gmail DRAFT (by draft_id). Never sends. Takes the "
    "same fields as create-draft; use it to revise a draft after the user asks for changes."
)

DELETE_DRAFT = (
    "Delete a Gmail DRAFT by draft_id. Only ever removes a draft — it cannot touch real "
    "(sent or received) mail. Use when the user abandons a draft."
)
