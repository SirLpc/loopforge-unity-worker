"""Short-lived LoopForge delivery authorization, shared by the relay and the Worker."""

import hashlib
import hmac
import json
import re


DIGEST = r"[0-9a-f]{64}"
GIT_ID = r"(?:[0-9a-f]{40}|[0-9a-f]{64})"
UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
SLOT = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
ARTIFACT = r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}"
FIELDS = {
    "project_id": r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", "workflow_id": r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}",
    "workflow_run_id": r"run-[A-Za-z0-9_-]{1,120}", "task_id": UUID, "test_job_id": UUID,
    "pool_id": SLOT, "slot_id": SLOT, "base_commit": GIT_ID, "revision": GIT_ID, "plan_digest": DIGEST,
    "approval_artifact_id": ARTIFACT, "approval_digest": DIGEST, "test_artifact_id": ARTIFACT, "test_digest": DIGEST,
}
ATTEMPTS = {"accept_attempt": 1, "implementation_attempt": 0}


class ApprovalError(ValueError):
    pass


def validate_claims(claims):
    if not isinstance(claims, dict) or claims.keys() != FIELDS.keys() | ATTEMPTS.keys():
        raise ApprovalError("Invalid approval claim fields")
    for name, pattern in FIELDS.items():
        if not isinstance(claims[name], str) or not re.fullmatch(pattern, claims[name]):
            raise ApprovalError("Invalid approval identifier or digest")
    for name, minimum in ATTEMPTS.items():
        if type(claims[name]) is not int or not minimum <= claims[name] <= 2**31 - 1:
            raise ApprovalError("Invalid approval attempt")


def signature(claims, issued, key):
    if not isinstance(key, str) or len(key) < 32 or not key.isascii() or not key.isprintable() or " " in key:
        raise ApprovalError("An independent protected signing key is required")
    data = json.dumps({"claims": claims, "issued_at": issued}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hmac.new(key.encode(), data.encode(), hashlib.sha256).hexdigest()


def sign_approval(claims, key, now):
    validate_claims(claims)
    issued = int(now)
    return {"claims": dict(claims), "issued_at": issued, "signature": signature(claims, issued, key)}


def authenticate_approval(envelope, key):
    if not isinstance(envelope, dict) or envelope.keys() != {"claims", "issued_at", "signature"}:
        raise ApprovalError("Invalid approval envelope")
    claims, issued, supplied = envelope["claims"], envelope["issued_at"], envelope["signature"]
    validate_claims(claims)
    if (type(issued) is not int or not 0 <= issued <= 2**63 - 1 or not isinstance(supplied, str)
            or not re.fullmatch(DIGEST, supplied) or not hmac.compare_digest(signature(claims, issued, key), supplied)):
        raise ApprovalError("Invalid approval signature")
    return dict(claims)


def check_freshness(envelope, now):
    if not -5 <= int(now) - envelope["issued_at"] <= 30:
        raise ApprovalError("Approval is outside its validity window")


def verify_approval(envelope, key, now):
    claims = authenticate_approval(envelope, key)
    check_freshness(envelope, now)
    return claims
