"""
Per-user OAuth enforcement for the /mcp endpoint.

Implements the gateway side of specs/connector-oauth.md (D1-v2, in
willcrestpartners/clark): every MCP call must carry a Cognito access token
proving a specific Willcrest person authorized via Google SSO. The verified
email replaces the old self-asserted caller_email parameter — tools resolve
identity through caller_email() below, never from tool arguments.

How a token is verified (stdlib only — no new dependencies):

  1. Structural claims check. The JWT payload is base64url-decoded (NOT
     signature-verified locally) and `iss`, `token_use == "access"`,
     `client_id == CONNECTOR_CLIENT_ID` and `exp` are enforced.
  2. Authenticity + email. The token is presented to the Cognito Hosted UI's
     /oauth2/userInfo endpoint. Cognito verifies its own token's signature,
     expiry and revocation state server-side and returns the user's email.
     A token that userInfo accepts is a genuine, unmodified Cognito token, so
     the claims read in step 1 are authentic.

  This deliberately replaces local JWKS/RS256 verification: RSA signature
  checks would require a new third-party package (`cryptography`/PyJWT), and
  the userInfo round-trip is required anyway to resolve the email (Cognito
  access tokens carry no email claim). It is also stronger on one axis:
  userInfo rejects REVOKED tokens, which pure JWKS verification cannot see.
  Results are cached per token (bounded TTL) so steady-state traffic makes no
  extra network calls.

Cutover flag: CONNECTOR_AUTH_REQUIRED (default false). When false the
middleware still verifies and injects identity from a token when one is
present, but a token-less request falls through to the legacy self-asserted
`caller_email` tool argument (read from the request body; the argument is no
longer in any tool schema, but old connector configs still send it). When
true, /mcp without a valid token is rejected with 401 + WWW-Authenticate
pointing at the protected-resource metadata, per the MCP auth spec.

Only /mcp is touched. /send keeps its HMAC check, /health stays public, and
the poller does not run through this module at all.
"""

import base64
import contextvars
import hashlib
import json
import os
import time
import urllib.error
import urllib.request

# ── configuration (env; see infra/template.yaml parameters) ─────────────────

def auth_required() -> bool:
    """CONNECTOR_AUTH_REQUIRED — the cutover flag (default: not enforcing)."""
    return os.environ.get("CONNECTOR_AUTH_REQUIRED", "").strip().lower() in ("1", "true", "yes")


def dcr_shim_enabled() -> bool:
    """CONNECTOR_DCR_SHIM — serve the static-registration shim (default off).

    Off: the protected-resource metadata points straight at the Cognito
    issuer and claude.ai must be given the client id manually (connector
    Advanced settings). On: the gateway also serves authorization-server
    metadata + a /register endpoint that hands every registration request the
    pre-registered client id — only needed if the manual-credentials path in
    claude.ai turns out to be unavailable.
    """
    return os.environ.get("CONNECTOR_DCR_SHIM", "").strip().lower() in ("1", "true", "yes")


def _pool_id() -> str:
    return os.environ.get("CONNECTOR_COGNITO_POOL_ID", "").strip()


def _client_id() -> str:
    return os.environ.get("CONNECTOR_CLIENT_ID", "").strip()


def _cognito_domain() -> str:
    """Hosted UI base URL, e.g. https://willcrest-clark.auth.us-east-1.amazoncognito.com"""
    return os.environ.get("CONNECTOR_COGNITO_DOMAIN", "").strip().rstrip("/")


def issuer() -> str:
    """The pool's OIDC issuer (publishes /.well-known/openid-configuration)."""
    pool = _pool_id()
    if not pool or "_" not in pool:
        return ""
    region = pool.split("_", 1)[0]
    return f"https://cognito-idp.{region}.amazonaws.com/{pool}"


def configured() -> bool:
    """Whether the pool/client/domain trio needed to verify tokens is set."""
    return bool(_pool_id() and _client_id() and _cognito_domain())


# ── request-scoped identity ──────────────────────────────────────────────────
# Set by the middleware before the MCP app runs; tools read them via
# caller_email(). Contextvars propagate into tasks the MCP server spawns for
# the request, and stateless HTTP mode keeps each request self-contained.

_verified_email: contextvars.ContextVar = contextvars.ContextVar("verified_email", default=None)
_legacy_email: contextvars.ContextVar = contextvars.ContextVar("legacy_email", default=None)


def caller_email():
    """Resolve the caller for a tool invocation.

    Returns (email, None) on success or (None, user-facing error) when no
    identity is available. Precedence: token-verified email always wins; the
    legacy self-asserted caller_email argument is honored only while
    CONNECTOR_AUTH_REQUIRED is false (the cutover window).
    """
    email = _verified_email.get()
    if email:
        return email, None
    if not auth_required():
        legacy = _legacy_email.get()
        if legacy:
            return legacy, None
    return None, (
        "Could not determine who you are. Connect the Willcrest Clark connector "
        "with your Google login (claude.ai Settings -> Connectors) and try again."
    )


# ── token verification ───────────────────────────────────────────────────────

# sha256(token) -> (email, valid_until_epoch). Bounded: entries expire with the
# token (capped at 5 minutes so revocation lags at most that long) and the map
# is pruned on insert.
_token_cache: dict = {}
_TOKEN_CACHE_MAX = 256
_CACHE_TTL_CAP = 300  # seconds


def _b64url_decode(segment: str) -> bytes:
    pad = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + pad)


def _decode_claims(token: str):
    """Decode the JWT payload WITHOUT verifying the signature.

    Authenticity is established separately by the userInfo call — see the
    module docstring. Returns the claims dict or None if the token is not
    even JWT-shaped.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        claims = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
    except Exception:
        return None
    return claims if isinstance(claims, dict) else None


def _userinfo_email(token: str):
    """Present the token to Cognito's userInfo endpoint.

    Returns (email, None) when Cognito accepts the token, (None, reason)
    otherwise. Cognito validates signature/expiry/revocation server-side.
    """
    url = f"{_cognito_domain()}/oauth2/userInfo"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            return None, "token rejected by Cognito"
        return None, f"userInfo HTTP {e.code}"
    except Exception as e:
        return None, f"userInfo unreachable: {e}"
    email = str(data.get("email") or "").strip().lower()
    if not email:
        return None, "token accepted but no email claim (openid+email scopes required)"
    return email, None


def verify_token(token: str):
    """Full verification: structural claims + Cognito userInfo (cached).

    Returns (email, None) or (None, reason).
    """
    if not configured():
        return None, "connector OAuth is not configured on the gateway"

    claims = _decode_claims(token)
    if claims is None:
        return None, "not a JWT"
    now = time.time()
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or exp <= now:
        return None, "token expired"
    if claims.get("iss") != issuer():
        return None, "wrong issuer"
    if claims.get("token_use") != "access":
        return None, "not an access token"
    if claims.get("client_id") != _client_id():
        return None, "token was not issued to the clark-connector client"

    key = hashlib.sha256(token.encode("utf-8")).hexdigest()
    cached = _token_cache.get(key)
    if cached and cached[1] > now:
        return cached[0], None

    email, reason = _userinfo_email(token)
    if email is None:
        _token_cache.pop(key, None)
        return None, reason

    valid_until = min(float(exp), now + _CACHE_TTL_CAP)
    if len(_token_cache) >= _TOKEN_CACHE_MAX:
        for k in [k for k, v in _token_cache.items() if v[1] <= now]:
            _token_cache.pop(k, None)
        if len(_token_cache) >= _TOKEN_CACHE_MAX:
            _token_cache.clear()  # pathological; drop everything rather than grow
    _token_cache[key] = (email, valid_until)
    return email, None


# ── ASGI middleware for the /mcp mount ───────────────────────────────────────

def _bearer_from_scope(scope) -> str:
    for k, v in scope.get("headers", []):
        if k.decode("latin-1").lower() == "authorization":
            value = v.decode("latin-1").strip()
            if value.lower().startswith("bearer "):
                return value[7:].strip()
    return ""


def host_base(scope) -> str:
    """https://<host> from the request headers (Function-URL-rotation-proof)."""
    host = ""
    for k, v in scope.get("headers", []):
        if k.decode("latin-1").lower() == "host":
            host = v.decode("latin-1").strip()
            break
    return f"https://{host}" if host else ""


async def _read_body(receive):
    """Buffer the request body and return (body, replay_receive)."""
    chunks = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body"):
            break
    body = b"".join(chunks)

    sent = False

    async def replay():
        nonlocal sent
        if sent:
            # Body already replayed — hand control back to the real channel so
            # the app still sees http.disconnect (fabricating empty
            # http.request messages here would spin the transport forever).
            return await receive()
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return body, replay


def _legacy_caller_from_body(body: bytes):
    """Pull params.arguments.caller_email out of a JSON-RPC tools/call body.

    Back-compat for the cutover window only: the argument is gone from every
    tool schema (FastMCP drops unknown arguments), but connector configs
    predating OAuth still send it. Never consulted once a verified token
    identity exists or CONNECTOR_AUTH_REQUIRED is true.
    """
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("method") != "tools/call":
        return None
    args = (payload.get("params") or {}).get("arguments")
    if isinstance(args, dict):
        value = args.get("caller_email")
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return None


async def _unauthorized(scope, send, detail: str):
    metadata_url = f"{host_base(scope)}/.well-known/oauth-protected-resource"
    www = f'Bearer resource_metadata="{metadata_url}"'
    if detail != "missing token":
        www = f'Bearer error="invalid_token", error_description="{detail}", resource_metadata="{metadata_url}"'
    body = json.dumps({"error": "unauthorized", "detail": detail}).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": 401,
        "headers": [
            (b"content-type", b"application/json"),
            (b"www-authenticate", www.encode("latin-1")),
        ],
    })
    await send({"type": "http.response.body", "body": body})


def middleware(app):
    """Wrap the MCP mount: verify bearer tokens, inject identity, enforce.

    /send and /health are mounted BEFORE this in the Starlette route table and
    never pass through here.
    """

    async def wrapped(scope, receive, send):
        if scope["type"] != "http":
            await app(scope, receive, send)
            return

        token = _bearer_from_scope(scope)
        verified = None
        reason = "missing token"
        if token:
            verified, reason = verify_token(token)

        if verified is None and auth_required():
            await _unauthorized(scope, send, reason)
            return

        body, replay = await _read_body(receive)
        legacy = None if verified else _legacy_caller_from_body(body)

        v_token = _verified_email.set(verified)
        l_token = _legacy_email.set(legacy)
        try:
            await app(scope, replay, send)
        finally:
            _verified_email.reset(v_token)
            _legacy_email.reset(l_token)

    return wrapped


# ── OAuth discovery endpoints (mounted in server.build_app) ──────────────────
# claude.ai discovers the authorization server through RFC 9728
# protected-resource metadata: 401 -> WWW-Authenticate.resource_metadata ->
# this document -> the Cognito issuer's own OIDC metadata.

WELL_KNOWN_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Cache-Control": "public, max-age=3600",
}

_as_metadata_cache: dict = {}


def protected_resource_metadata(base_url: str) -> dict:
    auth_server = base_url if dcr_shim_enabled() else issuer()
    return {
        "resource": f"{base_url}/mcp",
        "authorization_servers": [auth_server] if auth_server else [],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["openid", "email", "profile"],
    }


def authorization_server_metadata(base_url: str):
    """Shim only: Cognito's OIDC metadata with registration pointed at us.

    Cognito does not implement RFC 7591 dynamic client registration. When the
    shim is on, the gateway advertises itself as the authorization server,
    mirrors the pool's real endpoints from its openid-configuration, and adds
    a registration_endpoint that statically answers with the pre-registered
    clark-connector client id (see register_response).
    """
    iss = issuer()
    if not iss:
        return None
    meta = _as_metadata_cache.get(iss)
    if meta is None:
        url = f"{iss}/.well-known/openid-configuration"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                meta = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None
        _as_metadata_cache[iss] = meta
    out = dict(meta)
    out["registration_endpoint"] = f"{base_url}/register"
    out.setdefault("code_challenge_methods_supported", ["S256"])
    return out


def register_response(_request_body: dict) -> dict:
    """Static RFC 7591 answer: every registration gets the one real client."""
    return {
        "client_id": _client_id(),
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }
