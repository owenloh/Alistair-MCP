"""OAuth flow for the Alistair MCP (single-user auto-approve), driven in-process.

Sets PUBLIC_BASE_URL before importing so the MCP builds in OAuth mode, then via
TestClient(asgi): discovery metadata, dynamic client registration, the full
authorization-code + PKCE flow, that a minted token authorizes /mcp, that the
static SERVICE_API_KEY still works, and that no token is rejected.
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
os.environ["MEMORY_DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="oauth_"), "m.db")

from app import config as _cfg
_cfg.get_settings.cache_clear()
from app import mcp_server as M
from app.main import asgi
from fastapi.testclient import TestClient

R = []
def check(name, cond):
    R.append((name, bool(cond)))

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "1"}}}
MH = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

check("OAUTH_ENABLED true when base url set", M.OAUTH_ENABLED is True)
check("oauth routes collected", len(M.OAUTH_PATHS) >= 3)

with TestClient(asgi) as cl:
    # --- discovery ---
    asm = cl.get("/.well-known/oauth-authorization-server")
    check("AS metadata 200", asm.status_code == 200)
    md = asm.json() if asm.status_code == 200 else {}
    check("AS metadata has issuer", "issuer" in md)
    check("AS metadata has authorization_endpoint", "authorization_endpoint" in md)
    check("AS metadata has token_endpoint", "token_endpoint" in md)
    check("AS metadata advertises registration", "registration_endpoint" in md)
    # RFC 9728 keys the resource metadata to the /mcp resource path
    prm = cl.get("/.well-known/oauth-protected-resource/mcp")
    check("protected-resource metadata 200", prm.status_code == 200)
    check("protected-resource points at this AS",
          prm.status_code == 200 and "authorization_servers" in prm.json())

    # --- /mcp auth gate ---
    rno = cl.post("/mcp", json=INIT, headers=MH, follow_redirects=False)
    check("/mcp no token -> 401", rno.status_code == 401)
    rstatic = cl.post("/mcp", json=INIT, headers={**MH, "Authorization": "Bearer static-key-xyz"}, follow_redirects=False)
    check("/mcp static SERVICE_API_KEY works", rstatic.status_code == 200)
    check("/mcp static -> alistair_assistant", rstatic.json().get("result", {}).get("serverInfo", {}).get("name") == "alistair_assistant")

    # --- dynamic client registration ---
    reg = cl.post("/register", json={
        "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "client_name": "claude.ai-probe",
        "scope": "alistair",
    })
    check("DCR -> 201", reg.status_code == 201)
    client = reg.json() if reg.status_code == 201 else {}
    cid = client.get("client_id")
    check("DCR returns client_id", bool(cid))

    # --- authorization-code + PKCE flow ---
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    redirect_uri = "https://claude.ai/api/mcp/auth_callback"
    az = cl.get("/authorize", params={
        "response_type": "code", "client_id": cid, "redirect_uri": redirect_uri,
        "code_challenge": challenge, "code_challenge_method": "S256",
        "state": "xyz123", "scope": "alistair",
    }, follow_redirects=False)
    check("authorize -> redirect (auto-approve)", az.status_code in (302, 307))
    loc = az.headers.get("location", "")
    qs = up.parse_qs(up.urlparse(loc).query)
    code = (qs.get("code") or [None])[0]
    check("authorize redirect carries code", bool(code))
    check("authorize preserves state", (qs.get("state") or [None])[0] == "xyz123")

    tok = cl.post("/token", data={
        "grant_type": "authorization_code", "code": code or "", "redirect_uri": redirect_uri,
        "client_id": cid, "code_verifier": verifier,
    })
    check("token exchange -> 200", tok.status_code == 200)
    tj = tok.json() if tok.status_code == 200 else {}
    access = tj.get("access_token")
    check("token response has access_token", bool(access))
    check("token response has refresh_token", bool(tj.get("refresh_token")))

    # --- the minted token authorizes /mcp ---
    rok = cl.post("/mcp", json=INIT, headers={**MH, "Authorization": f"Bearer {access}"}, follow_redirects=False)
    check("minted OAuth token authorizes /mcp", rok.status_code == 200)

    # --- wrong PKCE verifier is rejected ---
    az2 = cl.get("/authorize", params={
        "response_type": "code", "client_id": cid, "redirect_uri": redirect_uri,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "s2", "scope": "alistair",
    }, follow_redirects=False)
    code2 = (up.parse_qs(up.urlparse(az2.headers.get("location", "")).query).get("code") or [None])[0]
    badtok = cl.post("/token", data={
        "grant_type": "authorization_code", "code": code2 or "", "redirect_uri": redirect_uri,
        "client_id": cid, "code_verifier": "wrong-verifier",
    })
    check("wrong PKCE verifier rejected", badtok.status_code >= 400)

# --- results ---
print("=== RESULTS ===")
ok = True
for n, p in R:
    print(f"  {'PASS' if p else 'FAIL'}  {n}")
    ok = ok and p
print(f"\n{'ALL PASS' if ok else 'SOME FAILED'}  ({sum(1 for _, p in R if p)}/{len(R)})")
sys.exit(0 if ok else 1)
