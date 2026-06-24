# scripts/ — local helpers (not deployed)

## `get_google_token.py` — re-mint the Google refresh token

Fixes the `403 "Request had insufficient authentication scopes"` you hit on calendar
**create / update / delete event**. The old `GOOGLE_REFRESH_TOKEN` was minted read-only;
this mints one with Calendar **read+write**.

```bash
cd scripts
pip install -r requirements.txt
python get_google_token.py          # opens a browser → consent → prints the token
```

Then paste the printed value into **Railway → Variables → `GOOGLE_REFRESH_TOKEN`** and redeploy.

### Scopes

| Scope | Grants | In the script |
| --- | --- | --- |
| `https://www.googleapis.com/auth/calendar` | Calendar read **and write** (list/get/freebusy + create/update/delete events) | **on by default** — this is the fix |
| `https://www.googleapis.com/auth/gmail.readonly` | Read your mail | commented out (optional) |
| `https://www.googleapis.com/auth/gmail.compose` | Create / edit / delete drafts (also permits send) | commented out (optional) |

**Read + draft Gmail = `gmail.readonly` + `gmail.compose`.** Both are commented out because:
- **Alistair has no Gmail tools yet** — the scope alone does nothing until Gmail endpoints
  are built into the service. (Happy to build read+draft Gmail tools as a follow-up.)
- Gmail scopes are Google "restricted" scopes — slightly clunkier consent (unverified-app
  warning), and for an unpublished app the token would be short-lived.

Recommendation: **mint Calendar-only now** to unblock writes; add Gmail when the tools exist.

### Two gotchas that will bite you

1. **Publish the OAuth app to "Production".** In *Testing* status, Google refresh tokens
   expire after **7 days**. In *Production* they're long-lived (the ~6-month-inactivity
   behaviour). You'll click through an "unverified app" warning for your own account — fine
   for a personal single-user app.
2. **Same client for mint and refresh.** The token must be minted with the same
   `client_id`/`client_secret` the running service refreshes with. If you reuse your
   existing `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` (via `scripts/.env`), only
   `GOOGLE_REFRESH_TOKEN` changes in Railway. If you make a **new** Desktop client, also
   update `GOOGLE_CLIENT_ID` + `GOOGLE_CLIENT_SECRET` in Railway to match.

### `scripts/.env` (optional)

Only needed if you reuse your existing OAuth client instead of `client_secret.json`:

```
GOOGLE_CLIENT_ID=xxxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=xxxx
```

(If you get `redirect_uri_mismatch`, your client is a "Web application" type — either make
a "Desktop app" client, or add `http://localhost` to its authorised redirect URIs.)
