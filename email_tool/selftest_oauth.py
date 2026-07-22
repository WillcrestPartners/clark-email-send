"""
Self-test for the /mcp per-user OAuth middleware (oauth.py + server.build_app).

Covers the connector-oauth spec's acceptance items that are testable without
AWS: 401s in enforced mode (missing/garbage/expired/wrong-client tokens, with
the WWW-Authenticate discovery pointer), verified-email injection, the
flag-off legacy caller_email fallback, forged-caller_email override, the
discovery documents, no caller_email in any tool schema, and /health + /send
(HMAC contract) staying untouched.

No network and no AWS: Cognito's userInfo endpoint is monkeypatched; tokens
are unsigned JWTs whose claims exercise the structural checks (authenticity
normally comes from Cognito accepting the token — here the monkeypatch stands
in for that). Needs the runtime deps (mcp/starlette), not pytest:

    pip install -r email_tool/requirements.txt
    python3 email_tool/selftest_oauth.py

Exits non-zero on the first failed check.
"""

import asyncio
import base64
import json
import os
import sys
import time

# ── environment BEFORE importing server (config + OAuth wiring) ──────────────

os.environ["APP_CONFIG_JSON"] = json.dumps({
    "global": {
        "sender_email": "clark@willcrestpartners.com",
        "default_daily_limit": 20,
        "confirmation_required": True,
        "copy_to_sent_folder": True,
    },
    "inbound": {"enabled": False, "poll_seconds": 300, "mailboxes": []},
    "users": {
        "bforster@willcrest.com": {"name": "Bret", "role": "admin", "daily_limit": 20, "active": True},
        "dnaas@willcrest.com": {"name": "Dominic", "role": "user", "daily_limit": 20, "active": True},
    },
})
os.environ["CLARK_INBOUND_HMAC_SECRET"] = "selftest-hmac-secret"
os.environ["CONNECTOR_COGNITO_POOL_ID"] = "us-east-1_SELFTEST"
os.environ["CONNECTOR_COGNITO_DOMAIN"] = "https://selftest.auth.us-east-1.amazoncognito.com"
os.environ["CONNECTOR_CLIENT_ID"] = "selftest-connector-client"
os.environ["CONNECTOR_AUTH_REQUIRED"] = "false"
# Register the shim routes at build time; individual tests flip the env var
# to check both metadata variants (the flag is read per-request).
os.environ["CONNECTOR_DCR_SHIM"] = "true"
os.environ["MCP_STATELESS_HTTP"] = "true"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import clark_client  # noqa: E402
import oauth  # noqa: E402
from server import build_app  # noqa: E402

ISSUER = oauth.issuer()
CLIENT_ID = os.environ["CONNECTOR_CLIENT_ID"]

# ── fakes ─────────────────────────────────────────────────────────────────────

# token string -> email accepted by the fake Cognito userInfo endpoint
_FAKE_USERINFO: dict = {}


def _fake_userinfo_email(token: str):
    email = _FAKE_USERINFO.get(token)
    if email is None:
        return None, "token rejected by Cognito"
    return email, None


oauth._userinfo_email = _fake_userinfo_email
# Pre-seed the authorization-server metadata cache so the shim route needs no
# network fetch of Cognito's real openid-configuration.
oauth._as_metadata_cache[ISSUER] = {
    "issuer": ISSUER,
    "authorization_endpoint": f"{os.environ['CONNECTOR_COGNITO_DOMAIN']}/oauth2/authorize",
    "token_endpoint": f"{os.environ['CONNECTOR_COGNITO_DOMAIN']}/oauth2/token",
    "jwks_uri": f"{ISSUER}/.well-known/jwks.json",
    "response_types_supported": ["code"],
}


_token_counter = 0


def make_token(client_id=CLIENT_ID, iss=ISSUER, token_use="access",
               exp_offset=3600, email="bforster@willcrest.com", register=True):
    """Unsigned JWT with the claims the structural check reads."""
    global _token_counter
    _token_counter += 1

    def b64(obj):
        raw = json.dumps(obj).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    header = b64({"alg": "RS256", "kid": "selftest"})
    payload = b64({
        "iss": iss,
        "client_id": client_id,
        "token_use": token_use,
        "exp": int(time.time()) + exp_offset,
        "sub": "selftest-sub",
        "scope": "openid email profile",
    })
    # Signature is fake (authenticity is the monkeypatched userInfo's job) but
    # must stay dot-free so the token still splits into exactly three parts,
    # and unique per call so two same-second tokens never collide.
    signature = b64({"sig": [client_id, exp_offset, email, _token_counter]})
    token = f"{header}.{payload}.{signature}"
    if register:
        _FAKE_USERINFO[token] = email
    return token


# ── minimal ASGI test client (stdlib only — no httpx/TestClient) ─────────────

class MiniClient:
    def __init__(self, app):
        self.app = app
        self._startup_done = asyncio.Event()
        self._shutdown_done = asyncio.Event()
        self._lifespan_task = None
        self._to_app = asyncio.Queue()

    async def __aenter__(self):
        scope = {"type": "lifespan", "asgi": {"version": "3.0"}}

        async def receive():
            return await self._to_app.get()

        async def send(message):
            if message["type"] == "lifespan.startup.complete":
                self._startup_done.set()
            elif message["type"] == "lifespan.shutdown.complete":
                self._shutdown_done.set()

        self._lifespan_task = asyncio.ensure_future(self.app(scope, receive, send))
        await self._to_app.put({"type": "lifespan.startup"})
        await asyncio.wait_for(self._startup_done.wait(), 15)
        return self

    async def __aexit__(self, *exc):
        await self._to_app.put({"type": "lifespan.shutdown"})
        await asyncio.wait_for(self._shutdown_done.wait(), 15)
        await self._lifespan_task

    async def request(self, method, path, headers=None, body=b""):
        headers = headers or {}
        raw_headers = [(k.lower().encode("latin-1"), v.encode("latin-1"))
                       for k, v in headers.items()]
        raw_headers.append((b"host", b"gateway.selftest.local"))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "root_path": "",
            "headers": raw_headers,
            "client": ("127.0.0.1", 12345),
            "server": ("gateway.selftest.local", 443),
        }

        sent = False
        status_headers = {}
        chunks = []
        done = asyncio.Event()

        async def receive():
            nonlocal sent
            if sent:
                # Like a real server: block until the response is finished,
                # then report the client hanging up.
                await done.wait()
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            if message["type"] == "http.response.start":
                status_headers["status"] = message["status"]
                status_headers["headers"] = {
                    k.decode("latin-1").lower(): v.decode("latin-1")
                    for k, v in message.get("headers", [])
                }
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
                if not message.get("more_body"):
                    done.set()

        await self.app(scope, receive, send)
        await asyncio.wait_for(done.wait(), 15)
        return status_headers["status"], status_headers["headers"], b"".join(chunks)


MCP_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}


def _parse_mcp(headers, body: bytes):
    """Return the JSON-RPC response object from a JSON or SSE MCP reply."""
    text = body.decode("utf-8", "replace")
    if "text/event-stream" in headers.get("content-type", ""):
        datas = [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")]
        text = datas[-1] if datas else "{}"
    try:
        return json.loads(text or "{}")
    except Exception:
        return {}


async def mcp_call(client, method, params=None, token=None, rpc_id=1):
    headers = dict(MCP_HEADERS)
    if token:
        headers["authorization"] = f"Bearer {token}"
    payload = {"jsonrpc": "2.0", "id": rpc_id, "method": method,
               "params": params or {}}
    status, hdrs, body = await client.request("POST", "/mcp", headers, json.dumps(payload).encode("utf-8"))
    return status, hdrs, _parse_mcp(hdrs, body)


def tool_text(rpc: dict) -> str:
    content = ((rpc.get("result") or {}).get("content")) or []
    return " ".join(c.get("text", "") for c in content if isinstance(c, dict))


PASSED = 0


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print(f"  OK  {name}")
    else:
        print(f"  FAIL {name}" + (f"\n      {detail}" if detail else ""))
        raise SystemExit(1)


# ── the tests ─────────────────────────────────────────────────────────────────

async def main():
    app = build_app(run_poller=False)
    async with MiniClient(app) as client:
        os.environ["CONNECTOR_AUTH_REQUIRED"] = "false"
        os.environ["CONNECTOR_DCR_SHIM"] = "false"

        # 0. Sanity: non-MCP surfaces untouched.
        status, _, body = await client.request("GET", "/health")
        check("/health stays public (200)", status == 200 and b'"status"' in body, body[:200])

        status, _, body = await client.request(
            "POST", "/send", {"content-type": "application/json",
                              "x-clark-signature": "sha256=bogus"}, b"{}")
        check("/send keeps its own HMAC gate (bad signature -> 401)", status == 401, f"{status} {body[:100]}")

        raw = b'{"not_to": 1}'
        good_sig = clark_client.sign(raw, os.environ["CLARK_INBOUND_HMAC_SECRET"])
        status, _, body = await client.request(
            "POST", "/send", {"content-type": "application/json",
                              "x-clark-signature": good_sig}, raw)
        check("/send HMAC contract unchanged (good signature -> 400 missing to)",
              status == 400 and b"missing" in body, f"{status} {body[:100]}")

        # 1. Discovery document (shim off): points at the Cognito issuer.
        status, hdrs, body = await client.request("GET", "/.well-known/oauth-protected-resource")
        meta = json.loads(body)
        check("protected-resource metadata served",
              status == 200 and meta.get("resource") == "https://gateway.selftest.local/mcp", body[:300])
        check("metadata names the Cognito issuer as authorization server",
              meta.get("authorization_servers") == [ISSUER], body[:300])
        status, _, body2 = await client.request("GET", "/.well-known/oauth-protected-resource/mcp")
        check("path-suffixed metadata variant served", status == 200 and json.loads(body2) == meta)

        # 2. Flag OFF: tools/list works with no token; schemas expose no caller_email.
        status, hdrs, rpc = await mcp_call(client, "tools/list")
        tools = (rpc.get("result") or {}).get("tools") or []
        check("flag off: tools/list without a token", status == 200 and len(tools) == 18,
              f"status={status} tools={len(tools)} err={rpc.get('error')}")
        offenders = [
            t["name"] for t in tools
            if "caller_email" in json.dumps(t.get("inputSchema", {}))
        ]
        check("no tool schema contains caller_email", not offenders, str(offenders))

        # 3. Flag OFF: legacy self-asserted caller_email still honored (cutover
        # back-compat — the argument is not in the schema but old configs send it).
        status, hdrs, rpc = await mcp_call(client, "tools/call", {
            "name": "check_my_access",
            "arguments": {"caller_email": "dnaas@willcrest.com"},
        })
        text = tool_text(rpc)
        check("flag off: legacy caller_email argument works with no token",
              status == 200 and "Access confirmed for dnaas@willcrest.com" in text, text[:300])

        # 4. Flag OFF: valid token wins over a forged caller_email argument.
        bret_token = make_token(email="bforster@willcrest.com")
        status, hdrs, rpc = await mcp_call(client, "tools/call", {
            "name": "check_my_access",
            "arguments": {"caller_email": "dnaas@willcrest.com"},
        }, token=bret_token)
        text = tool_text(rpc)
        check("token identity overrides forged caller_email",
              "Access confirmed for bforster@willcrest.com" in text, text[:300])

        # 5. Flag OFF: no identity at all -> clean tool error, not a crash.
        status, hdrs, rpc = await mcp_call(client, "tools/call", {
            "name": "check_my_access", "arguments": {},
        })
        text = tool_text(rpc)
        check("no identity -> connect-the-connector guidance",
              "Could not determine who you are" in text, text[:300])

        # ── ENFORCED MODE ────────────────────────────────────────────────────
        os.environ["CONNECTOR_AUTH_REQUIRED"] = "true"

        # 6. No token -> 401 with WWW-Authenticate pointing at the metadata.
        status, hdrs, rpc = await mcp_call(client, "tools/list")
        check("enforced: request without token -> 401", status == 401, str(status))
        www = hdrs.get("www-authenticate", "")
        check("401 carries WWW-Authenticate resource_metadata",
              "resource_metadata=" in www and "/.well-known/oauth-protected-resource" in www, www)

        # 7. Garbage / expired / wrong-client / non-access tokens -> 401.
        status, _, _ = await mcp_call(client, "tools/list", token="garbage.token.value")
        check("enforced: garbage token -> 401", status == 401, str(status))

        expired = make_token(exp_offset=-60)
        status, _, _ = await mcp_call(client, "tools/list", token=expired)
        check("enforced: expired token -> 401", status == 401, str(status))

        wrong_client = make_token(client_id="some-other-app-client")
        status, _, _ = await mcp_call(client, "tools/list", token=wrong_client)
        check("enforced: token from another app client -> 401", status == 401, str(status))

        id_token = make_token(token_use="id")
        status, _, _ = await mcp_call(client, "tools/list", token=id_token)
        check("enforced: id token (not access) -> 401", status == 401, str(status))

        cognito_rejected = make_token(register=False)  # structurally fine, userInfo says no
        status, _, _ = await mcp_call(client, "tools/list", token=cognito_rejected)
        check("enforced: token Cognito rejects -> 401", status == 401, str(status))

        # 8. Valid token -> full access, identity injected, forgery ignored.
        status, hdrs, rpc = await mcp_call(client, "tools/list", token=bret_token)
        tools = (rpc.get("result") or {}).get("tools") or []
        check("enforced: tools/list with valid token", status == 200 and len(tools) == 18,
              f"status={status} tools={len(tools)}")

        status, hdrs, rpc = await mcp_call(client, "tools/call", {
            "name": "check_my_access",
            "arguments": {"caller_email": "dnaas@willcrest.com"},
        }, token=bret_token)
        text = tool_text(rpc)
        check("enforced: verified identity injected; forged caller_email ignored",
              "Access confirmed for bforster@willcrest.com" in text, text[:300])

        # 9. Enforced: legacy fallback is dead (belt over the 401 braces —
        # middleware never sets it, and caller_email() refuses it regardless).
        tok = oauth._legacy_email.set("dnaas@willcrest.com")
        try:
            email, err = oauth.caller_email()
        finally:
            oauth._legacy_email.reset(tok)
        check("enforced: caller_email() refuses legacy identity", email is None and err, str(email))

        # 10. Second user attributes as themselves (per-user identity).
        dom_token = make_token(email="dnaas@willcrest.com")
        status, hdrs, rpc = await mcp_call(client, "tools/call", {
            "name": "check_my_access", "arguments": {},
        }, token=dom_token)
        text = tool_text(rpc)
        check("second user's token attributes to their own email",
              "Access confirmed for dnaas@willcrest.com" in text, text[:300])

        # 11. Health/send still fine while enforcement is on.
        status, _, _ = await client.request("GET", "/health")
        check("enforced: /health still public", status == 200)
        status, _, body = await client.request(
            "POST", "/send", {"content-type": "application/json",
                              "x-clark-signature": good_sig}, raw)
        check("enforced: /send still HMAC-only (no bearer needed)",
              status == 400 and b"missing" in body, f"{status} {body[:100]}")

        # 12. DCR shim (flag on): AS metadata mirrored + static /register.
        os.environ["CONNECTOR_DCR_SHIM"] = "true"
        status, _, body = await client.request("GET", "/.well-known/oauth-protected-resource")
        meta = json.loads(body)
        check("shim on: metadata names the gateway as authorization server",
              meta.get("authorization_servers") == ["https://gateway.selftest.local"], body[:300])
        status, _, body = await client.request("GET", "/.well-known/oauth-authorization-server")
        as_meta = json.loads(body)
        check("shim on: AS metadata mirrors Cognito + local registration_endpoint",
              status == 200
              and as_meta.get("token_endpoint", "").endswith("/oauth2/token")
              and as_meta.get("registration_endpoint") == "https://gateway.selftest.local/register",
              body[:400])
        status, _, body = await client.request(
            "POST", "/register", {"content-type": "application/json"},
            json.dumps({"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]}).encode())
        reg = json.loads(body)
        check("shim on: /register answers with the static client id",
              status == 201 and reg.get("client_id") == CLIENT_ID, body[:300])
        os.environ["CONNECTOR_DCR_SHIM"] = "false"

    print(f"\nAll {PASSED} checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
