"""Descriptions for the WhatsApp connector tools (read + draft only).

Same plain, behaviour-first style as the other connectors. The read tools proxy to a
small agent on {owner}'s laptop (online-only); the draft tool returns a wa.me link and
NEVER sends.
"""

CHATS = (
    "List {owner}'s recent WhatsApp chats (chat id/jid, name, last-message time, unread). "
    "Read-only, and ONLINE-ONLY: it reads from the WhatsApp agent on {owner}'s laptop, so it "
    "only works while that laptop is on — if it's offline the tool says so plainly (it never "
    "fabricates). Use a returned chat id with whatsapp/messages to read that conversation."
)

READ = (
    "Read recent messages in one WhatsApp chat by its chat id/jid (from the chats list). "
    "Read-only, online-only (via the laptop agent). Summarise for {owner}; it is his private "
    "messaging — never repeat secrets, OTP/2FA codes, or passwords you see."
)

SEARCH = (
    "Search {owner}'s WhatsApp messages by text. Read-only, online-only (via the laptop agent). "
    "Returns matching messages with their chat id so you can read the full thread."
)

RECENT = (
    "WhatsApp inbox — the most recent chats with a last-message preview and unread count, "
    "newest first. Read-only, online-only (via the laptop agent). Best for 'what's new on "
    "WhatsApp' / 'any new messages' — one call, no need to open each chat."
)

FIND = (
    "Find a WhatsApp chat by CONTACT NAME or phone number and read its recent messages in one "
    "step. Read-only, online-only. Resolves e.g. 'Chloe' (or a number) to the right chat — use "
    "this instead of guessing from message-text search. Returns the resolved {jid,name,number} "
    "plus recent messages, or says so if nothing matches."
)

DRAFT = (
    "Draft a WhatsApp message — returns a wa.me link that opens {owner}'s NORMAL WhatsApp with "
    "the text PRE-FILLED in the compose box for him to review and SEND HIMSELF. It NEVER "
    "sends, and needs no laptop/session. Give 'to' as a phone number (any format; a bare "
    "local number uses the default country code) or a contact name (resolved via the laptop "
    "agent if it's online), and 'body' as the message. 1:1 chats only. Write in {owner}'s voice, "
    "keep it tight, then hand him the link."
)
