"""
HTTP client for the Clark destination (the AI command-bus host).

Implements the cross-repo contract:
  - sign(): HMAC-SHA256 over the raw body, formatted as "sha256=<hexdigest>".
  - fetch_authorized_senders(): GET the allow-list (signed over the empty body),
    cached in memory with a short TTL.
  - post_envelope(): POST the inbound envelope JSON with the signature header,
    with a small retry/backoff on transient network errors.

Stdlib only (hmac/hashlib/json/time/urllib). No Anthropic key, no Clark DB.
"""

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request

# Module-level cache for the authorized-sender list.
_senders_cache: list = []
_senders_cache_ts: float = 0.0
_DEFAULT_TTL = int(os.environ.get("CLARK_SENDERS_TTL", "300"))


def sign(raw, secret: str) -> str:
    """Return 'sha256=<hexdigest>' for an HMAC-SHA256 over raw using secret."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    digest = hmac.new(secret, raw, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify(raw, secret: str, signature_header: str) -> bool:
    """Constant-time verification of an 'sha256=<hex>' signature header."""
    if not signature_header:
        return False
    expected = sign(raw, secret)
    return hmac.compare_digest(expected, signature_header.strip())


def _request(url: str, *, data: bytes = None, headers: dict = None, method: str = "GET",
             timeout: float = 15.0, retries: int = 3, backoff: float = 0.5):
    """Issue an HTTP request with a small retry/backoff on network errors.

    Returns (status_code, body_bytes, response_headers_dict).
    HTTP error responses (4xx/5xx) are returned, not retried (except 5xx).
    """
    last_exc = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                return resp.status, body, resp_headers
        except urllib.error.HTTPError as e:
            body = e.read()
            resp_headers = {k.lower(): v for k, v in (e.headers or {}).items()}
            # Retry server errors; surface client errors immediately.
            if 500 <= e.code < 600 and attempt < retries - 1:
                last_exc = e
                time.sleep(backoff * (2 ** attempt))
                continue
            return e.code, body, resp_headers
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_exc = e
            if attempt < retries - 1:
                time.sleep(backoff * (2 ** attempt))
                continue
            raise
    if last_exc:
        raise last_exc


def fetch_authorized_senders(url: str, secret: str, ttl: int = None,
                             force: bool = False) -> list:
    """GET the authorized-sender allow-list, cached in memory with a TTL.

    The gateway signs the EMPTY string for this read (per the contract).
    Returns a list of lowercased email strings. On error, returns the last
    cached list (possibly empty) so a transient failure does not open the gate.
    """
    global _senders_cache, _senders_cache_ts
    ttl = _DEFAULT_TTL if ttl is None else ttl

    now = time.time()
    if not force and _senders_cache_ts and (now - _senders_cache_ts) < ttl:
        return _senders_cache

    if not url:
        return _senders_cache

    headers = {"X-Clark-Signature": sign(b"", secret)}
    try:
        status, body, _ = _request(url, headers=headers, method="GET")
        if status // 100 != 2:
            return _senders_cache
        data = json.loads(body.decode("utf-8") or "{}")
        senders = [str(s).strip().lower() for s in data.get("senders", []) if s]
        _senders_cache = senders
        _senders_cache_ts = now
        return _senders_cache
    except Exception:
        # Defensive: keep the previous cache rather than failing the poll.
        return _senders_cache


def cached_senders() -> list:
    """Return the currently-cached allow-list without triggering a fetch."""
    return list(_senders_cache)


def senders_cache_age() -> float:
    """Seconds since the allow-list was last refreshed (inf if never)."""
    if not _senders_cache_ts:
        return float("inf")
    return time.time() - _senders_cache_ts


def post_envelope(url: str, secret: str, envelope: dict):
    """POST the envelope as raw JSON bytes with the signature header.

    Returns (status_code, response_json_or_None). Treat any 2xx as success.
    """
    return post_signed(url, secret, envelope)


def post_signed(url: str, secret: str, obj: dict):
    """Signed POST of a JSON object (HMAC over the raw body).

    Returns (status_code, response_json_or_None). Used for the inbound envelope
    and the /api/connector/* CIM-intake endpoints.
    """
    raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Clark-Signature": sign(raw, secret),
    }
    status, body, _ = _request(url, data=raw, headers=headers, method="POST")
    try:
        parsed = json.loads(body.decode("utf-8")) if body else None
    except Exception:
        parsed = None
    return status, parsed


def get_signed(url: str, secret: str):
    """Signed GET (HMAC over the EMPTY body, per the contract).

    Returns (status_code, response_json_or_None). Used for the connector
    company-search endpoint.
    """
    headers = {"X-Clark-Signature": sign(b"", secret)}
    status, body, _ = _request(url, headers=headers, method="GET")
    try:
        parsed = json.loads(body.decode("utf-8")) if body else None
    except Exception:
        parsed = None
    return status, parsed
