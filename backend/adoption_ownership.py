import uuid


def _canonical_uuid(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("manifest ownership domain ID must be a canonical UUID")
    try:
        canonical = str(uuid.UUID(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(
            "manifest ownership domain ID must be a canonical UUID"
        ) from exc
    if value != canonical:
        raise ValueError("manifest ownership domain ID must be a canonical UUID")
    return canonical


def validated_manifest_ownership(manifest: object) -> dict | None:
    """Validate an optional frozen ownership record in an adoption manifest.

    Legacy manifests omit ``ownership`` and return ``None``. A present record is
    accepted only in the exact, non-project form used by explicit tenant plans.
    """

    if not isinstance(manifest, dict):
        raise ValueError("adoption manifest must be an object")
    if "ownership" not in manifest:
        return None

    ownership = manifest["ownership"]
    if not isinstance(ownership, dict) or set(ownership) != {
        "account",
        "domain_id",
        "project_id",
    }:
        raise ValueError("manifest ownership schema is invalid")

    account = ownership["account"]
    if not isinstance(account, str) or not account or account != account.strip():
        raise ValueError("manifest ownership account is invalid")
    domain_id = _canonical_uuid(ownership["domain_id"])
    if ownership["project_id"] is not None:
        raise ValueError("manifest ownership project must be absent")

    return {
        "account": account,
        "domain_id": domain_id,
        "project_id": None,
    }
