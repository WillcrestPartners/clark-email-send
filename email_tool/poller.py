"""
Inbound poll orchestration (Phase 1 email command bus).

poll_once() runs one sweep over every configured mailbox:
  1. refresh the authorized-sender allow-list (cached with TTL)
  2. list unread inbox messages
  3. for each message (defensively, one bad message never kills the loop):
       - fetch raw, parse headers
       - atomically CLAIM it (idempotency by RFC822 Message-ID, falling back
         to the Gmail message id) — a concurrent/overlapping sweep loses the
         claim and skips, so a message is never double-forwarded
       - sender gate: unknown sender -> mark read, confirm claim, NO reply
       - hygiene gate: drop reason -> mark read, confirm claim
       - else build envelope, persist row, POST to Clark
       - on 2xx: confirm claim + status=acked + mark the Gmail message read
       - on failure: RELEASE the claim and leave unread, so a later sweep
         genuinely retries it

This module holds NO LLM and NO business logic — only gating + transport.
"""

import datetime
import os
import uuid

import access_control
import audit_log
import clark_client
import email_parse
import gateway_gate
import gmail_client
import inbound_store

# Module-level marker the /health route and diagnostics surface.
LAST_SUCCESSFUL_POLL = None


def _attachment_meta(msg) -> list:
    """Envelope attachment METADATA (filename/content_type/size_bytes) only.

    Attachment CONTENT is deliberately never forwarded — CIMs and other
    documents enter Clark through the Claude CIM-intake skill, not email. The
    metadata exists solely so Clark's reply can say "your PDF was not
    processed; use the CIM intake skill" instead of silently ignoring it."""
    return [
        {
            "filename": att["filename"],
            "content_type": att["content_type"],
            "size_bytes": att["size"],
        }
        for att in email_parse.extract_attachments(msg)
    ]


def _utc_now_iso() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


def _resolve_destination(dest: dict) -> tuple:
    """Resolve webhook + authorized-senders URLs from config or env."""
    dest = dest or {}
    webhook = dest.get("webhook_url") or os.environ.get("CLARK_WEBHOOK_URL", "")
    senders_url = dest.get("authorized_senders_url") or os.environ.get(
        "CLARK_AUTHORIZED_SENDERS_URL", ""
    )
    name = dest.get("name", "clark-os")
    return name, webhook, senders_url


def _process_message(mailbox: str, msg_id: str, dest_name: str, webhook_url: str,
                     secret: str, allowed: list, summary: dict) -> None:
    """Process a single message. Exceptions are caught by the caller."""
    msg = gmail_client.get_message_raw(mailbox, msg_id)
    parsed = email_parse.parse_message(msg, mailbox=mailbox)
    headers = email_parse.header_dict(msg)

    rfc822_id = parsed.get("rfc822_message_id", "")
    from_email = parsed["from"]["email"]

    # Idempotency: atomically claim the message. Messages without a Message-ID
    # header fall back to the (mailbox-stable) Gmail message id, so nothing is
    # exempt from dedup. Losing the claim means another sweep is handling (or
    # has handled) it.
    dedup_key = rfc822_id or f"gmail:{mailbox}:{msg_id}"
    if not inbound_store.claim_message(dedup_key):
        summary["skipped_seen"] += 1
        return

    # Sender gate — unknown senders are silently dropped (mark read, no reply).
    if not gateway_gate.sender_allowed(from_email, allowed):
        gmail_client.mark_read(mailbox, msg_id)
        inbound_store.confirm_claim(dedup_key, "ignored")
        audit_log.log_attempt(from_email, mailbox, parsed.get("subject", ""),
                              "ignored", reason="sender_not_authorized")
        summary["ignored_sender"] += 1
        return

    # Hygiene gate — deterministic hard rules.
    drop_reason = gateway_gate.hygiene_drop_reason(headers)
    if drop_reason:
        gmail_client.mark_read(mailbox, msg_id)
        inbound_store.confirm_claim(dedup_key, "dropped")
        audit_log.log_attempt(from_email, mailbox, parsed.get("subject", ""),
                              "dropped", reason=drop_reason)
        summary["dropped_hygiene"] += 1
        return

    # Build the envelope.
    gateway_message_id = str(uuid.uuid4())
    envelope = {
        "envelope_version": "1",
        "gateway_message_id": gateway_message_id,
        "mailbox": mailbox,
        "received_at": _utc_now_iso(),
        "from": parsed["from"],
        "to": parsed["to"],
        "subject": parsed["subject"],
        "rfc822_message_id": rfc822_id,
        "instruction_text": parsed["instruction_text"],
        "body_text": parsed["body_text"],
        "body_full_text": parsed["body_full_text"],
        "signature_block": parsed["signature_block"],
    }

    # Attachment metadata only (never content) — lets Clark's reply point the
    # sender at the CIM-intake skill. Text-only mail keeps the prior envelope.
    attachments = _attachment_meta(msg)
    if attachments:
        envelope["attachments"] = attachments

    # Persist as received -> gated -> routed before POSTing.
    inbound_store.record(
        gateway_message_id=gateway_message_id,
        rfc822_message_id=rfc822_id,
        mailbox=mailbox,
        from_addr=from_email,
        received_at=envelope["received_at"],
        gate_result="passed",
        status="routed",
        attempts=0,
        destination=dest_name,
        raw_ref=msg_id,
    )

    # POST to Clark.
    try:
        status_code, _resp = clark_client.post_envelope(webhook_url, secret, envelope)
    except Exception as e:
        # Release the claim so the (still-unread) message retries next sweep.
        inbound_store.release_claim(dedup_key)
        inbound_store.update_status(gateway_message_id, "send_error", attempts=1)
        audit_log.log_attempt(from_email, mailbox, parsed.get("subject", ""),
                              "failed", reason=f"post_envelope: {e}")
        summary["failed"] += 1
        return

    if 200 <= status_code < 300:
        inbound_store.confirm_claim(dedup_key, "acked")
        inbound_store.update_status(gateway_message_id, "acked", attempts=1)
        gmail_client.mark_read(mailbox, msg_id)
        audit_log.log_attempt(from_email, mailbox, parsed.get("subject", ""),
                              "acked", message_id=gateway_message_id)
        summary["acked"] += 1
    else:
        # Release the claim and leave unread so a future sweep retries.
        inbound_store.release_claim(dedup_key)
        inbound_store.update_status(gateway_message_id, "rejected", attempts=1)
        audit_log.log_attempt(from_email, mailbox, parsed.get("subject", ""),
                              "failed", reason=f"clark status {status_code}")
        summary["failed"] += 1


def poll_once() -> dict:
    """Run one poll sweep across all configured mailboxes. Returns a summary."""
    global LAST_SUCCESSFUL_POLL

    summary = {
        "mailboxes": 0,
        "listed": 0,
        "acked": 0,
        "skipped_seen": 0,
        "ignored_sender": 0,
        "dropped_hygiene": 0,
        "failed": 0,
        "errors": [],
    }

    inbound = access_control.get_inbound_config()
    if not inbound.get("enabled"):
        summary["errors"].append("inbound disabled")
        return summary

    secret = os.environ.get("CLARK_INBOUND_HMAC_SECRET", "")

    for mb in inbound.get("mailboxes", []):
        mailbox = mb.get("address")
        if not mailbox:
            continue
        summary["mailboxes"] += 1
        dest_name, webhook_url, senders_url = _resolve_destination(mb.get("destination", {}))

        # Refresh the allow-list (cached with TTL inside the client).
        allowed = clark_client.fetch_authorized_senders(senders_url, secret)

        try:
            msg_ids = gmail_client.list_unread_message_ids(mailbox)
        except Exception as e:
            summary["errors"].append(f"{mailbox}: list failed: {e}")
            audit_log.log_attempt("system", mailbox, "", "failed",
                                  reason=f"list_unread: {e}")
            continue

        summary["listed"] += len(msg_ids)
        for msg_id in msg_ids:
            try:
                _process_message(mailbox, msg_id, dest_name, webhook_url,
                                  secret, allowed, summary)
            except Exception as e:
                # One bad message must never kill the loop.
                summary["errors"].append(f"{mailbox}/{msg_id}: {e}")
                audit_log.log_attempt("system", mailbox, "", "failed",
                                      reason=f"process {msg_id}: {e}")

    LAST_SUCCESSFUL_POLL = _utc_now_iso()
    # On Lambda the poller and the /health web endpoint run in different
    # containers, so a module global is invisible to /health — persist the
    # marker in the shared state store too (best-effort).
    try:
        import state_store
        if state_store.enabled():
            state_store.set_meta("last_successful_poll", LAST_SUCCESSFUL_POLL)
    except Exception:
        pass
    return summary


def last_successful_poll() -> str:
    try:
        import state_store
        if state_store.enabled():
            return state_store.get_meta("last_successful_poll") or LAST_SUCCESSFUL_POLL
    except Exception:
        pass
    return LAST_SUCCESSFUL_POLL
