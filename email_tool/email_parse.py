"""
Pure email-parsing helpers — NO I/O.

Turns a raw RFC822 message (bytes/str) or a Gmail API 'full' payload into the
fields Clark's inbound envelope needs:
  - From (name + email), To, Subject, Message-ID
  - body_full_text   : the entire message including quoted history
  - body_text /
    instruction_text : the latest message only, with quoted history stripped
  - signature_block  : a conservative heuristic signature, or ''

Everything here is deterministic and side-effect free so it is trivially
testable and safe to call from the poll loop.
"""

import base64
import email
import re
from email.header import decode_header, make_header
from email.utils import parseaddr


# ── low-level decoding ──────────────────────────────────────────────────────

def _decode_header_value(value) -> str:
    """Decode an RFC2047-encoded header into a plain unicode string."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def _b64url_decode(data: str) -> bytes:
    if not data:
        return b""
    # Gmail uses URL-safe base64; pad to a multiple of 4.
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


# ── extracting plain text ───────────────────────────────────────────────────

def _text_from_email_message(msg) -> str:
    """Return the best-effort plain-text body of an email.message.Message."""
    if msg.is_multipart():
        # Prefer the first text/plain part not flagged as an attachment.
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            disp = str(part.get("Content-Disposition", "")).lower()
            if "attachment" in disp:
                continue
            if part.get_content_type() == "text/plain":
                return _decode_part(part)
        # Fall back to text/html if no text/plain exists.
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                return _strip_html(_decode_part(part))
        return ""
    if msg.get_content_type() == "text/html":
        return _strip_html(_decode_part(msg))
    return _decode_part(msg)


def _decode_part(part) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, TypeError):
        return payload.decode("utf-8", errors="replace")


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")


def _strip_html(html: str) -> str:
    text = re.sub(r"(?is)<br\s*/?>", "\n", html)
    text = re.sub(r"(?is)</p\s*>", "\n\n", text)
    text = _TAG_RE.sub("", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
    )
    return text


# ── quoted-history stripping ────────────────────────────────────────────────

# Common reply separators ("On <date> <person> wrote:", Outlook "From:" blocks).
_REPLY_SEPARATORS = [
    re.compile(r"^\s*On .+ wrote:\s*$", re.IGNORECASE),
    re.compile(r"^\s*On .+,.+ at .+ wrote:\s*$", re.IGNORECASE),
    re.compile(r"^\s*-+\s*Original Message\s*-+\s*$", re.IGNORECASE),
    re.compile(r"^\s*-+\s*Forwarded message\s*-+\s*$", re.IGNORECASE),
    re.compile(r"^\s*From:\s*.+$", re.IGNORECASE),  # Outlook header block
    re.compile(r"^_{5,}\s*$"),  # Outlook horizontal rule
    re.compile(r"^\s*Sent from my \w+", re.IGNORECASE),
]


def strip_quoted_history(text: str) -> str:
    """Return only the latest message — drop quoted/forwarded history.

    Conservative: cut at the first recognised reply separator, and drop
    leading-'>' quote lines. If everything would be stripped, fall back to
    the original text so we never emit an empty instruction by mistake.
    """
    if not text:
        return ""

    lines = text.splitlines()
    kept = []
    for line in lines:
        if any(sep.match(line) for sep in _REPLY_SEPARATORS):
            break
        if line.lstrip().startswith(">"):
            # A run of quote lines marks the start of history.
            break
        kept.append(line)

    result = "\n".join(kept).strip()
    return result if result else text.strip()


# ── signature heuristic ─────────────────────────────────────────────────────

_SIG_CUES = re.compile(
    r"(?i)(\bphone\b|\bmobile\b|\bcell\b|\btel\b|\bdirect\b|"
    r"\b[A-Z]{2,}\b|@|http|www\.|\bllc\b|\binc\b|\bpartners?\b|"
    r"\bcfo\b|\bceo\b|\bcoo\b|\bpresident\b|\bdirector\b|\bmanager\b|"
    r"\bassociate\b|\bprincipal\b|\bfounder\b|\+\d)"
)
_PHONE_RE = re.compile(r"(\+?\d[\d\-\.\s\(\)]{6,}\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def extract_signature_block(text: str) -> str:
    """Heuristic signature extraction. Returns '' when unsure.

    1. Honour the RFC 3676 '-- ' delimiter line if present.
    2. Otherwise, look at a trailing block of short lines that contain
       phone/email/title cues.
    """
    if not text:
        return ""

    lines = text.splitlines()

    # 1) Standard '-- ' sig delimiter.
    for i, line in enumerate(lines):
        if line.rstrip() == "--" or line == "-- ":
            sig = "\n".join(lines[i + 1:]).strip()
            return sig

    # 2) Trailing short-line block with contact cues.
    # Walk backwards over non-empty trailing lines.
    end = len(lines)
    while end > 0 and not lines[end - 1].strip():
        end -= 1
    if end == 0:
        return ""

    start = end
    while start > 0:
        line = lines[start - 1]
        if not line.strip():
            # Allow a single blank line inside the block, then require cues.
            break
        if len(line) > 60:
            break
        start -= 1

    block_lines = [l for l in lines[start:end] if l.strip()]
    if not block_lines:
        return ""

    block = "\n".join(block_lines).strip()
    cue_hits = sum(1 for l in block_lines if _SIG_CUES.search(l))
    has_contact = bool(_PHONE_RE.search(block) or _EMAIL_RE.search(block))
    # Require at least two cue-bearing lines or a contact detail, and keep it short.
    if len(block_lines) <= 8 and (cue_hits >= 2 or has_contact):
        return block
    return ""


# ── attachment extraction ───────────────────────────────────────────────────

def extract_attachments(raw_or_payload) -> list:
    """Return the message's attachment parts as dicts (deterministic, no I/O).

    Each dict: {filename, content_type, size, bytes}. A part counts as an
    attachment if its Content-Disposition is 'attachment' or it carries a
    filename (covers inline-but-named parts). Multipart containers and the
    text/plain|html body parts are skipped. Transport concerns (base64, size
    caps, which types to forward) are the caller's — this stays a pure parser.
    """
    msg = _to_email_message(raw_or_payload)
    out = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disp = str(part.get("Content-Disposition", "")).lower()
        filename = _decode_header_value(part.get_filename() or "")
        if "attachment" not in disp and not filename:
            continue  # a body part, not an attachment
        data = part.get_payload(decode=True)
        if not data:
            continue
        out.append({
            "filename": filename or "attachment",
            "content_type": (part.get_content_type() or "application/octet-stream").lower(),
            "size": len(data),
            "bytes": data,
        })
    return out


# ── envelope assembly ───────────────────────────────────────────────────────

def _headers_from_payload(payload: dict) -> dict:
    """Flatten a Gmail 'full' payload's headers list into a dict (last wins)."""
    headers = {}
    for h in (payload.get("payload", {}) or payload).get("headers", []) or []:
        headers[h.get("name", "").lower()] = h.get("value", "")
    return headers


def _to_email_message(raw_or_payload):
    """Accept raw bytes/str (RFC822) or a Gmail message dict; return Message."""
    if isinstance(raw_or_payload, (bytes, bytearray)):
        return email.message_from_bytes(bytes(raw_or_payload))
    if isinstance(raw_or_payload, str):
        return email.message_from_string(raw_or_payload)
    if isinstance(raw_or_payload, dict):
        # Gmail format='raw' returns {'raw': <b64url>}; 'full' returns a payload.
        if raw_or_payload.get("raw"):
            return email.message_from_bytes(_b64url_decode(raw_or_payload["raw"]))
        # format='full': reconstruct text from the part tree is non-trivial,
        # so we only support 'raw' for the body here. Callers should pass raw.
        raise ValueError("Gmail 'full' payload without 'raw' is not supported; request format='raw'.")
    raise TypeError(f"Unsupported message type: {type(raw_or_payload)!r}")


def header_dict(raw_or_payload) -> dict:
    """Return a lowercased header dict for gating, from raw bytes/str or payload."""
    if isinstance(raw_or_payload, dict) and not raw_or_payload.get("raw"):
        return _headers_from_payload(raw_or_payload)
    msg = _to_email_message(raw_or_payload)
    return {k.lower(): v for k, v in msg.items()}


def parse_message(raw_or_payload, mailbox: str = "clark@willcrestpartners.com") -> dict:
    """Parse a raw RFC822 message into Clark inbound-envelope fields.

    Returns a dict with: from {name,email}, to [..], subject, rfc822_message_id,
    body_full_text, body_text, instruction_text, signature_block.
    (gateway_message_id / received_at / mailbox / version are added by the caller.)
    """
    msg = _to_email_message(raw_or_payload)

    from_raw = _decode_header_value(msg.get("From", ""))
    from_name, from_email = parseaddr(from_raw)
    from_name = _decode_header_value(from_name)

    to_raw = _decode_header_value(msg.get("To", ""))
    to_list = [addr.strip() for addr in to_raw.split(",") if addr.strip()] or [mailbox]

    subject = _decode_header_value(msg.get("Subject", ""))
    message_id = (msg.get("Message-ID") or msg.get("Message-Id") or "").strip()

    body_full = _text_from_email_message(msg).replace("\r\n", "\n").strip()
    latest = strip_quoted_history(body_full)
    signature = extract_signature_block(latest) or extract_signature_block(body_full)

    return {
        "from": {"name": from_name or "", "email": (from_email or "").strip()},
        "to": to_list,
        "subject": subject,
        "rfc822_message_id": message_id,
        "body_full_text": body_full,
        "body_text": latest,
        "instruction_text": latest,
        "signature_block": signature or "",
    }
