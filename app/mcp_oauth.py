"""Single-user OAuth 2.1 authorization server for the Alistair MCP.

claude.ai custom connectors require OAuth (not bearer-only). Alistair is
single-user (just Owen), so this is the minimal correct shape:

  * **Dynamic Client Registration** is open, so claude.ai can self-register.
  * **/authorize auto-approves** — there is exactly one user, so it mints an
    authorization code immediately with no consent screen (PKCE is still
    enforced by the SDK on exchange).
  * The static **SERVICE_API_KEY is also accepted as a bearer token**, so the
    existing clients (the Pipecat voice shell, Claude Desktop/Code, Cursor,
    Gemini CLI) keep working through this same auth path.

Stores are in-memory: tokens reset on redeploy, and claude.ai silently re-runs
the flow when a token stops working — fine for a single human-paced user. (A
durable store can move into the SQLite layer later if multi-tenancy is wanted.)
"""
from __future__ import annotations

import secrets
import time

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

SCOPES = ["alistair"]
ACCESS_TTL = 3600        # 1h access tokens
CODE_TTL = 600           # 10m authorization codes


class SingleUserOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    def __init__(self, service_key_getter):
        # callable returning the current SERVICE_API_KEY (read lazily so a rotated
        # key takes effect without reconstructing the provider)
        self._service_key = service_key_getter
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._access: dict[str, AccessToken] = {}
        self._refresh: dict[str, RefreshToken] = {}

    # ---- dynamic client registration ----
    async def get_client(self, client_id: str):
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull):
        self._clients[client_info.client_id] = client_info

    # ---- authorization (auto-approve: one user, no consent UI) ----
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        code = "ac_" + secrets.token_urlsafe(32)
        self._codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or SCOPES,
            expires_at=time.time() + CODE_TTL,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        ac = self._codes.get(authorization_code)
        if ac and ac.client_id == client.client_id and ac.expires_at > time.time():
            return ac
        return None

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        self._codes.pop(authorization_code.code, None)
        return self._issue(client.client_id, authorization_code.scopes)

    # ---- refresh ----
    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str):
        rt = self._refresh.get(refresh_token)
        if rt and rt.client_id == client.client_id:
            return rt
        return None

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        self._refresh.pop(refresh_token.token, None)
        return self._issue(client.client_id, scopes or refresh_token.scopes)

    # ---- access-token verification (also accepts the static service key) ----
    async def load_access_token(self, token: str):
        key = self._service_key()
        if key and secrets.compare_digest(token, key):
            return AccessToken(token=token, client_id="static-service-key", scopes=SCOPES, expires_at=None)
        at = self._access.get(token)
        if at and (at.expires_at is None or at.expires_at > time.time()):
            return at
        return None

    async def revoke_token(self, token) -> None:
        t = getattr(token, "token", token)
        self._access.pop(t, None)
        self._refresh.pop(t, None)

    # ---- helper ----
    def _issue(self, client_id: str, scopes) -> OAuthToken:
        scopes = list(scopes or SCOPES)
        at = "at_" + secrets.token_urlsafe(32)
        rt = "rt_" + secrets.token_urlsafe(32)
        self._access[at] = AccessToken(
            token=at, client_id=client_id, scopes=scopes, expires_at=int(time.time() + ACCESS_TTL)
        )
        self._refresh[rt] = RefreshToken(token=rt, client_id=client_id, scopes=scopes, expires_at=None)
        return OAuthToken(
            access_token=at, token_type="Bearer", expires_in=ACCESS_TTL,
            scope=" ".join(scopes), refresh_token=rt,
        )
