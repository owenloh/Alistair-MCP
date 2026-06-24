#!/usr/bin/env python3
"""Mint a Google OAuth refresh token for Alistair (Calendar read+write; optional Gmail).

Run this LOCALLY — it opens a browser for consent. It does NOT touch Railway; it just
prints a refresh token that you then paste into Railway as GOOGLE_REFRESH_TOKEN.

WHY: the current token was minted read-only, so calendar create/update/delete return
403 "insufficient authentication scopes". Re-minting with the Calendar scope below fixes it.

------------------------------------------------------------------------------------
ONE-TIME GOOGLE SETUP
  1. console.cloud.google.com -> APIs & Services -> Library -> enable "Google Calendar API"
     (and "Gmail API" too, only if you uncomment the Gmail scopes below).
  2. OAuth consent screen -> User type: External -> add your own Google account as a
     Test user -> then **PUBLISH the app to "Production"**.  <-- IMPORTANT
       * In "Testing" status, refresh tokens expire after 7 DAYS.
       * In "Production", they're long-lived (the ~6-month-inactivity behaviour).
       * You'll see an "unverified app" warning for your own account — click
         "Advanced -> go to <app> (unsafe)" to proceed. That's expected for a personal app.
  3. Credentials -> Create credentials -> OAuth client ID -> Application type
     "Desktop app" -> download the JSON as  scripts/client_secret.json
     (OR skip the file and reuse your EXISTING client by putting GOOGLE_CLIENT_ID and
      GOOGLE_CLIENT_SECRET in a local scripts/.env — but that client must be a Desktop
      client, or have http://localhost registered as a redirect URI.)

RUN
  cd scripts
  pip install -r requirements.txt
  python get_google_token.py
  # -> browser opens -> consent -> the refresh token is printed.
  # Paste it into Railway as GOOGLE_REFRESH_TOKEN and redeploy.

IMPORTANT: the refresh token must be minted with the SAME client_id/secret the running
service uses to refresh. If you minted with a NEW OAuth client, also update
GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in Railway to that same client.
------------------------------------------------------------------------------------
"""
from __future__ import annotations

import os
import pathlib
import sys

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    sys.exit("Missing dependencies. Run:  pip install -r requirements.txt")

# Optional: load scripts/.env so GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET can live there.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------------
# SCOPES — exactly what the refresh token will be allowed to do.
# ---------------------------------------------------------------------------------
SCOPES = [
    # Calendar read + WRITE. Covers list/get/freebusy AND create/update/delete events.
    # This single scope fixes the 403 you're hitting now.
    "https://www.googleapis.com/auth/calendar",

    # --- OPTIONAL: Gmail read + draft. Uncomment BOTH lines to include. ---------------
    # NOTE: Alistair has NO Gmail tools yet, so adding these only future-proofs the token
    # (nothing uses them until Gmail endpoints are built). Gmail scopes are "restricted":
    # you must enable the Gmail API and click through the unverified-app warning. Read +
    # draft = readonly + compose (compose also technically permits send).
    # "https://www.googleapis.com/auth/gmail.readonly",   # read your mail
    # "https://www.googleapis.com/auth/gmail.compose",    # create / edit / delete drafts
]

_HERE = pathlib.Path(__file__).resolve().parent
_CLIENT_FILE = _HERE / "client_secret.json"


def _build_flow() -> InstalledAppFlow:
    if _CLIENT_FILE.exists():
        print(f"Using OAuth client from {_CLIENT_FILE.name}")
        return InstalledAppFlow.from_client_secrets_file(str(_CLIENT_FILE), scopes=SCOPES)

    cid = os.environ.get("GOOGLE_CLIENT_ID")
    csec = os.environ.get("GOOGLE_CLIENT_SECRET")
    if not (cid and csec):
        sys.exit(
            "No client_secret.json found and GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set.\n"
            "Either download a Desktop OAuth client JSON to scripts/client_secret.json,\n"
            "or put the two values in scripts/.env."
        )
    print("Using OAuth client from GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET")
    client_config = {
        "installed": {
            "client_id": cid,
            "client_secret": csec,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    return InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)


def main() -> None:
    print("Requesting these scopes:")
    for s in SCOPES:
        print(f"  - {s}")
    print()

    flow = _build_flow()
    # access_type=offline + prompt=consent guarantees Google returns a refresh_token.
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    if not creds.refresh_token:
        sys.exit(
            "\nNo refresh_token was returned. Re-run and make sure you fully complete the\n"
            "consent screen. (prompt='consent' is already set, which forces a fresh token.)"
        )

    bar = "=" * 70
    print("\n" + bar)
    print("SUCCESS — paste this into Railway as  GOOGLE_REFRESH_TOKEN:")
    print(bar)
    print(creds.refresh_token)
    print(bar)
    print("Granted scopes: " + " ".join(creds.scopes or SCOPES))
    print(
        "\nReminder: if you minted with a NEW OAuth client, also set GOOGLE_CLIENT_ID and\n"
        "GOOGLE_CLIENT_SECRET in Railway to that same client, then redeploy."
    )


if __name__ == "__main__":
    main()
