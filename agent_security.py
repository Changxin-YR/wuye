"""Agent security primitives independent from the LLM/provider.

The model is never an authorization authority.  These helpers provide a stable
risk taxonomy, authorization fingerprints for short-lived delegated grants,
and minimum-disclosure serialization for data sent to an upstream model.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from typing import Any, Mapping

R0 = "R0"  # read only
R1 = "R1"  # ordinary reversible write
R2 = "R2"  # important relationship/state change
R3 = "R3"  # destructive, financial or authorization change

# R2 operations can be automated only when the business service has already
# resolved one unambiguous target and applies its normal scope/state checks.
DEFAULT_R2_AUTO = {
    "relation.bind_by_name",
    "lease.create",
    "parking.assign",
}


def r2_auto_commands() -> set[str]:
    """Return the deploy-time R2 automation allow-list.

    Only commands already classified as R2 can become automatic. An empty
    environment variable disables all R2 auto execution. This is deliberately
    an allow-list: configuration can make the policy stricter, never turn R3
    into an automatic action.
    """
    raw = os.getenv("AGENT_R2_AUTO_COMMANDS")
    if raw is None:
        return set(DEFAULT_R2_AUTO)
    requested = {item.strip() for item in raw.split(",") if item.strip()}
    return {command for command in requested if RISK_BY_COMMAND.get(command) == R2}

RISK_BY_COMMAND = {
    # Legacy compatibility commands
    "house.add": R1,
    "house.bind": R2,
    "house.unbind": R3,
    "house.delete": R3,
    "notice.add": R1,
    "notice.edit": R1,
    "notice.delete": R3,
    "user.disable": R3,
    "user.enable": R3,
    # Property V2 domain commands
    "community.save": R1,
    "building.save": R1,
    "unit.save": R1,
    "house.save": R1,
    "house.ownership": R2,
    "property.archive": R3,
    "person.save": R1,
    "person.archive": R3,
    "relation.bind": R2,
    "relation.bind_by_name": R2,
    "relation.end": R2,
    "lease.create": R2,
    "lease.checkout": R2,
    "staff.create": R3,
    "staff.roles": R3,
    "staff.state": R3,
    "role.save": R3,
    "order.create": R1,
    "order.assign": R1,
    "order.reassign": R2,
    "order.accept": R1,
    "order.progress": R1,
    "order.finish": R1,
    "order.close": R2,
    "order.reopen": R2,
    "order.cancel": R2,
    "order.evaluate": R1,
    "complaint.create": R1,
    "complaint.assign": R1,
    "complaint.resolve": R1,
    "complaint.close": R2,
    "notice.save": R1,
    "notice.archive": R2,
    "visitor.create": R1,
    "visitor.checkin": R1,
    "visitor.checkout": R1,
    "visitor.cancel": R1,
    "vehicle.save": R1,
    "vehicle.archive": R2,
    "parking.save": R1,
    "parking.assign": R2,
    "parking.release": R2,
    "device.save": R1,
    "device.archive": R2,
    "inspection.create": R1,
    "inspection.complete": R1,
    "fee.save": R2,
    "bill.create": R2,
    "bill.batch": R3,
    "bill.void": R3,
    "payment.record": R3,
    "payment.reverse": R3,
}


def risk_for(command: str, *, high_impact: bool = False, read_only: bool = False) -> str:
    if read_only:
        return R0
    return RISK_BY_COMMAND.get(command, R3 if high_impact else R1)


def execution_mode(command: str, *, high_impact: bool = False, read_only: bool = False) -> str:
    risk = risk_for(command, high_impact=high_impact, read_only=read_only)
    if risk == R0:
        return "READ_ONLY"
    if risk == R1 or (risk == R2 and command in r2_auto_commands()):
        return "AUTO"
    return "CONFIRM"


def requires_confirmation(command: str, *, high_impact: bool = False) -> bool:
    return execution_mode(command, high_impact=high_impact) == "CONFIRM"


def authorization_fingerprint(policy: Any) -> str:
    """Hash the *current* server-side IAM state, not any model-provided role."""
    identity = policy.identity()
    payload = {
        "user_id": getattr(policy.user, "id", None),
        "auth_version": getattr(policy.user, "auth_version", None),
        "active": bool(getattr(policy.user, "active", False)),
        "roles": identity.get("roles", []),
        "permissions": identity.get("permissions", []),
        "data_scope": identity.get("data_scope", []),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def issue_agent_token(policy: Any) -> str:
    """Issue an opaque short-lived token carrying a non-secret IAM fingerprint.

    The database still stores only SHA-256(token).  The embedded fingerprint
    lets callbacks detect out-of-band RBAC/scope edits even if auth_version was
    not incremented by a legacy/manual database operation.
    """
    return f"ag1.{secrets.token_urlsafe(32)}.{authorization_fingerprint(policy)[:16]}"


def validate_agent_token_fingerprint(token: str, policy: Any) -> bool:
    # Existing grants from older versions remain valid until their short expiry.
    if not isinstance(token, str) or not token.startswith("ag1."):
        return True
    parts = token.split(".")
    if len(parts) != 3 or not re.fullmatch(r"[A-Za-z0-9_-]{32,64}", parts[1]) or not re.fullmatch(r"[0-9a-f]{16}", parts[2]):
        return False
    return hmac.compare_digest(parts[2], authorization_fingerprint(policy)[:16])


def mask_phone(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    compact = re.sub(r"\s+", "", value)
    if len(compact) < 7:
        return "***"
    return compact[:3] + "****" + compact[-4:]


def mask_reference(value: Any) -> Any:
    if not isinstance(value, str) or not value:
        return value
    if len(value) <= 6:
        return "***"
    return value[:2] + "***" + value[-4:]


def safe_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Minimum-disclosure record intended for model context/tool output."""
    hidden = {
        "password", "password_hash", "token", "token_hash", "payload",
        "auth_version", "failed_logins", "locked_until", "emergency_contact",
        "before_data", "after_data",
    }
    result: dict[str, Any] = {}
    for key, value in record.items():
        if key in hidden:
            continue
        if key in {"phone", "contact_phone"}:
            result[key] = mask_phone(value)
        elif key in {"reference"}:
            result[key] = mask_reference(value)
        else:
            result[key] = value
    return result


def redact_provider_text(text: Any, *secret_values: str) -> str:
    if not isinstance(text, str):
        return ""
    out = text
    for secret in secret_values:
        if isinstance(secret, str) and secret:
            out = out.replace(secret, "[临时授权已隐藏]")
    # Defensive catch for a copied current-generation grant. This does not try
    # to recognize arbitrary credentials; it only matches our own grant format.
    out = re.sub(r"ag1\.[A-Za-z0-9_-]{32,64}\.[0-9a-f]{16}", "[临时授权已隐藏]", out)
    # Compatibility defense for a pre-upgrade Dify conversation quoting an
    # older opaque grant after the application has already switched formats.
    out = re.sub(
        r"(?i)(request_token\s*[:：=]\s*)[A-Za-z0-9_.-]{32,100}",
        r"\1[临时授权已隐藏]",
        out,
    )
    return out


def error_code_for(status: int, message: str = "") -> str:
    if status == 401:
        return "AUTH_EXPIRED"
    if status == 403 and ("数据范围" in message or "负责" in message or "可访问范围" in message):
        return "DATA_SCOPE_DENIED"
    if status == 403:
        return "PERMISSION_DENIED"
    if status == 404:
        return "RESOURCE_NOT_FOUND"
    if status == 409 and ("同名" in message or "多套" in message or "歧义" in message):
        return "AMBIGUOUS_ENTITY"
    if status == 409:
        return "BUSINESS_CONFLICT"
    if status == 410:
        return "CONFIRMATION_EXPIRED"
    if status == 400 and "缺少必要参数" in message:
        return "MISSING_PARAMETER"
    if status == 429:
        return "RATE_LIMITED"
    if status == 400:
        return "VALIDATION_ERROR"
    return "SYSTEM_ERROR" if status >= 500 else "REQUEST_ERROR"
