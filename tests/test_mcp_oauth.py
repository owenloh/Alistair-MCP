"""OAuth flow for the Alistair MCP, now with the operator approval gate.

Sets PUBLIC_BASE_URL + an approval password before importing so the MCP builds in
OAuth mode, then via TestClient(asgi) drives: discovery, dynamic client
registration, and the FULL gated flow — /authorize now redirects to the consent
page (no auto-approve), a wrong password is rejected, the right password mints the
code, the txn is single-use, PKCE is enforced, the minted token authorizes /mcp,
and the static SERVICE_API_KEY still works. Also checks the approval-secret
resolution (dedicated password > service key > none = fail closed).
"""
import base64
import hashlib
import os
import secrets
import sys
import tempfile
import urllib.parse as up

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["PUBLIC_BASE_URL"] = "https://testserver"  # SDK requires an https issuer
os.environ["SERVICE_API_KEY"] = "static-key-xyz"
os.environ["OAUTH_APPROVAL_PASSWORD"] = "approve-me-123"
os.environ["MEMORY_DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="oauth_"), "m.db")

from app import config as _cfg
_cfg.get_settings.cache_clear()
from app import mcp_server as M
from app.config import Settings
from app.main import asgi
from app.mcp_oauth import SingleUserOAuthProvider
from fastapi.testclient import TestClient

PW = "approve-me-123"
R = []
def check(name, cond):
    R.append((name, bool(cond)))

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "1"}}}
MH = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
REDIRECT = "https://claude.ai/api/mcp/auth_callback"

def authorize_to_consent(cl, cid, challenge, state):
    """GET /authorize -> follow to /oauth/consent, return the txn (no code yet)."""
    az = cl.get("/authorize", params={
        "response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256",
        "state": state, "scope": "alistair",
    }, follow_redirects=False)
    loc = az.headers.get("location", "")
    return az, loc, (up.parse_qs(up.urlparse(loc).query).get("txn") or [None])[0]

# --- config: approval-secret resolution (fail closed) ---
check("approval secret prefers dedicated password",
      Settings(oauth_approval_password="pw", service_api_key="sk").oauth_approval_secret == "pw")
check("approval secret falls back to service key",
      Settings(oauth_approval_password=None, service_api_key="sk").oauth_approval_secret == "sk")
check("approval secret None when neither set",
      Settings(oauth_approval_password=None, service_api_key=None).oauth_approval_secret is None)
check("verify_approval fails closed with no secret",
      SingleUserOAuthProvider(lambda: None, lambda: None, "https://x").verify_approval("anything") is False)

check("OAUTH_ENABLED true when base url set", M.OAUTH_ENABLED is True)

with TestClient(asgi) as cl:
    # --- discovery ---
    md = cl.get("/.well-known/oauth-authorization-server")
    check("AS metadata 200 + endpoints", md.status_code == 200 and all(
        k in md.json() for k in ("issuer", "authorization_endpoint", "token_endpoint", "registration_endpoint")))
    prm = cl.get("/.well-known/oauth-protected-resource/mcp")
    check("protected-resource metadata 200", prm.status_code == 200 and "authorization_servers" in prm.json())

    # --- /mcp auth gate ---
    check("/mcp no token -> 401", cl.post("/mcp", json=INIT, headers=MH, follow_redirects=False).status_code == 401)
    rstatic = cl.post("/mcp", json=INIT, headers={**MH, "Authorization": "Bearer static-key-xyz"}, follow_redirects=False)
    check("/mcp static SERVICE_API_KEY works",
          rstatic.status_code == 200 and rstatic.json().get("result", {}).get("serverInfo", {}).get("name") == "alistair_assistant")

    # --- dynamic client registration ---
    reg = cl.post("/register", json={
        "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "client_name": "claude.ai-probe", "scope": "alistair"})
    cid = reg.json().get("client_id") if reg.status_code == 201 else None
    check("DCR -> 201 + client_id", reg.status_code == 201 and bool(cid))

    # --- the gate: /authorize must NOT auto-approve ---
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    az, loc, txn = authorize_to_consent(cl, cid, challenge, "xyz123")
    check("authorize -> redirect", az.status_code in (302, 307))
    check("authorize redirects to /oauth/consent (not the client)", "/oauth/consent" in loc)
    check("authorize leaks NO code", "code=" not in loc)
    check("consent txn issued", bool(txn))

    form = cl.get("/oauth/consent", params={"txn": txn})
    check("consent page renders", form.status_code == 200 and "Approve" in form.text)

    # wrong password -> no code, stays on the form
    bad = cl.post("/oauth/consent", data={"txn": txn, "password": "WRONG"}, follow_redirects=False)
    check("wrong password rejected (no redirect, no code)",
          bad.status_code == 200 and "Incorrect" in bad.text and "code=" not in bad.headers.get("location", ""))

    # right password -> redirect to the client callback with a code
    good = cl.post("/oauth/consent", data={"txn": txn, "password": PW}, follow_redirects=False)
    cloc = good.headers.get("location", "")
    qs = up.parse_qs(up.urlparse(cloc).query)
    code = (qs.get("code") or [None])[0]
    check("approved -> redirect to client", good.status_code in (302, 307) and cloc.startswith(REDIRECT))
    check("approved redirect carries code", bool(code))
    check("state preserved through the gate", (qs.get("state") or [None])[0] == "xyz123")

    # txn is single-use
    reuse = cl.post("/oauth/consent", data={"txn": txn, "password": PW}, follow_redirects=False)
    check("txn single-use (reuse -> 400)", reuse.status_code == 400)

    # --- token exchange + use ---
    tok = cl.post("/token", data={"grant_type": "authorization_code", "code": code or "",
                                  "redirect_uri": REDIRECT, "client_id": cid, "code_verifier": verifier})
    at = tok.json().get("access_token") if tok.status_code == 200 else None
    check("token exchange -> access+refresh", tok.status_code == 200 and bool(at) and bool(tok.json().get("refresh_token")))
    ro = cl.post("/mcp", json=INIT, headers={**MH, "Authorization": f"Bearer {at}"}, follow_redirects=False)
    check("minted OAuth token authorizes /mcp", ro.status_code == 200)

    # --- wrong PKCE verifier still rejected (through the gate) ---
    _, _, txn2 = authorize_to_consent(cl, cid, challenge, "s2")
    g2 = cl.post("/oauth/consent", data={"txn": txn2, "password": PW}, follow_redirects=False)
    code2 = (up.parse_qs(up.urlparse(g2.headers.get("location", "")).query).get("code") or [None])[0]
    badtok = cl.post("/token", data={"grant_type": "authorization_code", "code": code2 or "",
                                     "redirect_uri": REDIRECT, "client_id": cid, "code_verifier": "wrong"})
    check("wrong PKCE verifier rejected", badtok.status_code >= 400)

print("=== RESULTS ===")
ok = True
for n, p in R:
    print(f"  {'PASS' if p else 'FAIL'}  {n}")
    ok = ok and p
print(f"\n{'ALL PASS' if ok else 'SOME FAILED'}  ({sum(1 for _, p in R if p)}/{len(R)})")
sys.exit(0 if ok else 1)
