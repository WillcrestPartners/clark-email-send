"""
Deterministic inbound gating — NO LLM, no business logic.

Two responsibilities:
  - sender_allowed(): is the sender on the authorized allow-list (case-insensitive)?
  - hygiene_drop_reason(): hard, deterministic rules that drop a message
    (auto-replies, bulk/list mail, bounces, self-loops). Returns a reason
    string to drop, or None to let it through.

Intent classification happens in Clark, not here.
"""

SENDER_SELF = "clark@willcrestpartners.com"


def sender_allowed(from_email: str, allowed_set) -> bool:
    """True if from_email is on the allow-list, compared case-insensitively."""
    if not from_email:
        return False
    addr = from_email.strip().lower()
    allowed = {a.strip().lower() for a in (allowed_set or [])}
    return addr in allowed


def _get(headers: dict, name: str) -> str:
    """Case-insensitive header fetch returning a stripped string."""
    if not headers:
        return ""
    target = name.lower()
    for k, v in headers.items():
        if k.lower() == target:
            return (v or "").strip()
    return ""


def hygiene_drop_reason(headers: dict, self_address: str = SENDER_SELF) -> str | None:
    """Return a drop reason string, or None if the message passes hygiene.

    Deterministic hard rules only — never call an LLM here.
    """
    headers = headers or {}

    # Loop guard: never act on mail the gateway itself sent.
    from_email = ""
    # Try to pull a bare email out of the From header.
    from_raw = _get(headers, "from").lower()
    if "<" in from_raw and ">" in from_raw:
        from_email = from_raw[from_raw.find("<") + 1: from_raw.find(">")].strip()
    else:
        from_email = from_raw
    if self_address.lower() in from_raw:
        return "self_loop"

    # Automated replies (vacation responders, etc.).
    auto = _get(headers, "auto-submitted").lower()
    if auto and auto != "no":
        return f"auto-submitted:{auto}"

    # Bulk / list / junk precedence.
    precedence = _get(headers, "precedence").lower()
    if precedence in ("bulk", "list", "junk"):
        return f"precedence:{precedence}"

    # Mailing-list traffic.
    if _get(headers, "list-id"):
        return "list-id"
    if _get(headers, "list-unsubscribe"):
        return "list-unsubscribe"

    # Bounce / delivery-status notifications.
    if "mailer-daemon" in from_raw or "postmaster" in from_raw:
        return "bounce:sender"
    content_type = _get(headers, "content-type").lower()
    if "multipart/report" in content_type:
        return "bounce:multipart-report"

    return None
