"""
DynamoDB-backed state for the Lambda deployment.

The always-on ECS deployment kept two kinds of state in the local process:
an SQLite table for inbound-message idempotency/audit, and an in-memory dict
for per-user daily send counts. Neither survives Lambda's ephemeral, possibly
concurrent invocations, so on Lambda we externalise both into one DynamoDB
table.

This module is only used when GATEWAY_TABLE is set (Lambda). Local/dev runs
leave it unset and fall back to inbound_store's SQLite + access_control's
in-memory counts. Import is lazy so boto3 is never required for local dev.

Single-table layout (PK attribute name: "pk"):
  - inbound message rows:  pk = "MSG#<gateway_message_id>", attr rfc822_message_id
  - daily send counters:   pk = "COUNT#<email>#<YYYY-MM-DD>", attr count (Number)
A sparse GSI "rfc822-index" (PK rfc822_message_id) powers already_seen() without
a scan. All rows carry a "ttl" epoch so DynamoDB expires bookkeeping data.
"""

import datetime
import os

_TTL_DAYS = int(os.environ.get("GATEWAY_TTL_DAYS", "90"))
_table = None


def enabled() -> bool:
    return bool(os.environ.get("GATEWAY_TABLE"))


def _tbl():
    global _table
    if _table is None:
        import boto3  # lazy: only present/needed in the Lambda runtime
        _table = boto3.resource("dynamodb").Table(os.environ["GATEWAY_TABLE"])
    return _table


def _now_iso() -> str:
    return datetime.datetime.utcnow().isoformat()


def _ttl_epoch() -> int:
    return int((datetime.datetime.utcnow() + datetime.timedelta(days=_TTL_DAYS)).timestamp())


# ── inbound message idempotency / audit (mirrors inbound_store's API) ────────

def already_seen(rfc822_message_id: str) -> bool:
    if not rfc822_message_id:
        return False
    resp = _tbl().query(
        IndexName="rfc822-index",
        KeyConditionExpression="rfc822_message_id = :r",
        ExpressionAttributeValues={":r": rfc822_message_id},
        Limit=1,
        Select="COUNT",
    )
    return resp.get("Count", 0) > 0


def record(
    gateway_message_id: str,
    rfc822_message_id: str,
    mailbox: str,
    from_addr: str,
    received_at: str,
    gate_result: str,
    status: str,
    attempts: int = 0,
    destination: str = None,
    raw_ref: str = None,
) -> None:
    item = {
        "pk": f"MSG#{gateway_message_id}",
        "gateway_message_id": gateway_message_id,
        "mailbox": mailbox,
        "from_addr": from_addr,
        "received_at": received_at,
        "gate_result": gate_result,
        "status": status,
        "attempts": attempts,
        "created_at": _now_iso(),
        "ttl": _ttl_epoch(),
    }
    # rfc822_message_id feeds the sparse GSI — only set when present.
    if rfc822_message_id:
        item["rfc822_message_id"] = rfc822_message_id
    if destination is not None:
        item["destination"] = destination
    if raw_ref is not None:
        item["raw_ref"] = raw_ref
    _tbl().put_item(Item=item)


def update_status(gateway_message_id: str, status: str, attempts: int = None) -> None:
    expr = "SET #s = :s"
    names = {"#s": "status"}
    values = {":s": status}
    if attempts is not None:
        expr += ", attempts = :a"
        values[":a"] = attempts
    _tbl().update_item(
        Key={"pk": f"MSG#{gateway_message_id}"},
        UpdateExpression=expr,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def recent(limit: int = 20) -> list:
    """Recent inbound message rows (dashboard/diagnostics). Low-volume scan."""
    resp = _tbl().scan(
        FilterExpression="begins_with(pk, :p)",
        ExpressionAttributeValues={":p": "MSG#"},
        Limit=max(limit * 5, 25),
    )
    rows = resp.get("Items", [])
    rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return rows[:limit]


# ── daily send counts (mirrors access_control's in-memory counters) ──────────

def _count_key(email: str, day: str) -> str:
    return f"COUNT#{email.lower()}#{day}"


def get_daily_count(email: str, day: str) -> int:
    resp = _tbl().get_item(Key={"pk": _count_key(email, day)})
    item = resp.get("Item")
    return int(item["count"]) if item and "count" in item else 0


def increment_daily_count(email: str, day: str) -> int:
    """Atomically increment and return the new count for email on day."""
    resp = _tbl().update_item(
        Key={"pk": _count_key(email, day)},
        UpdateExpression="ADD #c :one SET #t = if_not_exists(#t, :ttl)",
        ExpressionAttributeNames={"#c": "count", "#t": "ttl"},
        ExpressionAttributeValues={":one": 1, ":ttl": _ttl_epoch()},
        ReturnValues="UPDATED_NEW",
    )
    return int(resp["Attributes"]["count"])
