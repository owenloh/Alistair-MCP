"""Descriptions for the media connector tools (open a link, transcribe a video).

Single source of truth: both the HTTP router and the MCP @mcp.tool wrappers import
these, so every client sees the identical contract. Behaviour-first, read-only, with
the transcription fallback made explicit.
"""

OPEN_LINK = (
    "Open ANY web link for {user} and return its readable content: the final URL "
    "(after redirects), HTTP status, page title, meta/OpenGraph description, and a "
    "plain-text excerpt of the body with the HTML stripped. YouTube links are also "
    "enriched with their public oEmbed (title/author/thumbnail). Use this when {user} "
    "pastes a URL and asks 'what's on this page', 'open this', 'read this article', or "
    "wants a link summarised. Read-only — it only fetches public content and posts "
    "nothing. Then summarise in Alistair's voice; don't dump the raw excerpt. "
    "max_chars caps the returned body text (default 4000). For the SPOKEN words in a "
    "YouTube/Instagram video, use transcribe_video instead."
)

TRANSCRIBE = (
    "Transcribe a YouTube or Instagram video link — return what is actually SAID in it. "
    "Use this for 'transcribe this', 'what does this reel/short say', or to summarise a "
    "video from its URL. For YouTube it pulls the video's own caption track directly "
    "(no download, no key) and returns the full text plus timed segments, title and "
    "author; pass an optional lang (e.g. 'en') to prefer a caption language. Instagram, "
    "audio, and caption-less YouTube have no free caption track, so they route to an "
    "optional external speech-to-text agent (TRANSCRIBE_AGENT_URL); if that isn't "
    "configured the tool returns a clear 503 saying so rather than guessing — it NEVER "
    "fabricates a transcript. Read-only. Summarise for {user}; don't dump the raw transcript."
)
