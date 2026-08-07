"""Process the complete live non-Ceph adoption queue without arbitrary batching."""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import time
from collections.abc import Iterable

import main as app_main
from fastapi import HTTPException
from adopt_one import OperatorStop, Target, _load_live_catalog, run_one
from database import AdoptionClaim, get_session, init_db


_CEPH_PATTERN = re.compile(r"ceph", re.IGNORECASE)


def _parse_approved_ips(values: Iterable[str]) -> dict[tuple[str, int], str]:
    result: dict[tuple[str, int], str] = {}
    for value in values:
        match = re.fullmatch(r"([^/]+)/net(0|[1-9][0-9]*)=(.+)", value)
        if match is None or ":" not in match.group(1):
            raise OperatorStop("approved_ip_argument_invalid")
        proxmox_id = match.group(1)
        device_id = int(match.group(2))
        raw_ip = match.group(3)
        try:
            parsed = ipaddress.ip_address(raw_ip)
        except ValueError as exc:
            raise OperatorStop("approved_ip_argument_invalid") from exc
        if parsed.version != 4 or str(parsed) != raw_ip:
            raise OperatorStop("approved_ip_argument_invalid")
        key = (proxmox_id, device_id)
        if key in result or raw_ip in result.values():
            raise OperatorStop("approved_ip_argument_duplicate")
        result[key] = raw_ip
    return result


def _fresh_catalog(timeout_seconds: int = 300) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while True:
        catalog = _load_live_catalog(app_main.settings.api_auth_token)
        freshness = catalog.get("freshness") or {}
        if (
            freshness.get("inventory_collection_current") is True
            and freshness.get("nic_collection_current") is True
        ):
            return catalog
        if time.monotonic() >= deadline:
            raise OperatorStop("candidate_inventory_not_current")
        time.sleep(2)


def _candidate_map(catalog: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for candidate in catalog.get("candidates", []):
        if not isinstance(candidate, dict):
            raise OperatorStop("candidate_catalog_invalid")
        proxmox_id = candidate.get("proxmox_id")
        if not isinstance(proxmox_id, str) or proxmox_id in result:
            raise OperatorStop("candidate_catalog_identity_invalid")
        result[proxmox_id] = candidate
    return result


def _active_claims() -> dict[str, AdoptionClaim]:
    session = get_session()
    try:
        result = {}
        for claim in session.query(AdoptionClaim).all():
            if claim.state in {"managed", "retired"}:
                continue
            proxmox_id = f"{claim.proxmox_cluster}:{claim.proxmox_vmid}"
            if proxmox_id in result:
                raise OperatorStop("multiple_active_claims_for_proxmox_id")
            result[proxmox_id] = claim
        return result
    finally:
        session.close()


def _manifest_for(candidate: dict | None, claim: AdoptionClaim | None) -> dict:
    if claim is not None:
        try:
            manifest = json.loads(claim.manifest_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise OperatorStop("active_claim_manifest_invalid") from exc
    else:
        manifest = ((candidate or {}).get("adoption_plan") or {}).get("manifest")
    if not isinstance(manifest, dict):
        raise OperatorStop("candidate_manifest_unavailable")
    return manifest


def _required_external_devices(manifest: dict) -> set[int]:
    result = set()
    for network in manifest.get("networks") or []:
        device = network.get("device") if isinstance(network, dict) else None
        if (
            isinstance(device, str)
            and re.fullmatch(r"net(0|[1-9][0-9]*)", device)
            and network.get("ip") is None
            and network.get("ip_allocation", "cloudstack") == "external"
            and network.get("ip_override_required") is True
        ):
            result.add(int(device[3:]))
    return result


def _candidate_is_actionable(candidate: dict) -> bool:
    blockers = set(candidate.get("blockers") or [])
    return (
        candidate.get("vm_type") == "qemu"
        and candidate.get("template") is False
        and candidate.get("disposition") in {"ready", "blocked"}
        and blockers <= {"adoption_executor_not_enabled"}
        and isinstance(
            ((candidate.get("adoption_plan") or {}).get("manifest_sha256")),
            str,
        )
    )


def _safe_result(
    proxmox_id: str,
    name: str,
    outcome: str,
    *,
    details: object = None,
) -> dict:
    row: dict[str, object] = {
        "proxmox_id": proxmox_id,
        "name": name,
        "outcome": outcome,
    }
    if details is not None:
        row["details"] = details
    return row


def _safe_http_exception(exc: HTTPException) -> dict:
    detail: object = exc.detail
    if isinstance(detail, str):
        safe_detail: object = detail
    elif isinstance(detail, dict):
        safe_detail = {
            key: value
            for key, value in detail.items()
            if key in {"message", "blockers"}
            and (
                isinstance(value, str)
                or (
                    isinstance(value, list)
                    and all(isinstance(item, str) for item in value)
                )
            )
        }
    else:
        safe_detail = "http_exception_detail_not_string_or_mapping"
    return {"status_code": exc.status_code, "detail": safe_detail}


def run_complete_queue(
    approved_ips: dict[tuple[str, int], str],
    *,
    timeout_seconds: int,
    poll_seconds: float,
) -> dict:
    init_db(app_main.settings.database_url)
    initial_catalog = _fresh_catalog()
    initial_candidates = _candidate_map(initial_catalog)
    active_claims = _active_claims()
    queue_ids = sorted(set(initial_candidates) | set(active_claims))
    outcomes = []
    print(
        json.dumps(
            {
                "event": "queue_discovered",
                "scope": "all_current_non_ceph_plus_active_claims",
                "total_catalog_and_claim_ids": len(queue_ids),
            },
            sort_keys=True,
        ),
        flush=True,
    )

    for index, proxmox_id in enumerate(queue_ids, start=1):
        initial_candidate = initial_candidates.get(proxmox_id)
        active_claim = active_claims.get(proxmox_id)
        name = str((initial_candidate or {}).get("name") or "")
        if not name and active_claim is not None:
            try:
                name = str(json.loads(active_claim.manifest_json).get("name") or "")
            except (TypeError, json.JSONDecodeError):
                name = ""
        print(
            json.dumps(
                {
                    "event": "candidate_considered",
                    "index": index,
                    "total": len(queue_ids),
                    "proxmox_id": proxmox_id,
                    "name": name,
                },
                sort_keys=True,
            ),
            flush=True,
        )

        if _CEPH_PATTERN.search(name):
            outcomes.append(_safe_result(proxmox_id, name, "excluded_ceph"))
            continue

        if active_claim is None:
            if initial_candidate is None:
                outcomes.append(
                    _safe_result(proxmox_id, name, "blocked", details=["candidate_missing"])
                )
                continue
            if initial_candidate.get("disposition") == "existing_external":
                outcomes.append(_safe_result(proxmox_id, name, "already_external"))
                continue
            if not _candidate_is_actionable(initial_candidate):
                outcomes.append(
                    _safe_result(
                        proxmox_id,
                        name,
                        "blocked",
                        details=initial_candidate.get("blockers") or [
                            initial_candidate.get("disposition") or "not_actionable"
                        ],
                    )
                )
                continue

        try:
            current_catalog = _fresh_catalog()
            current_candidate = _candidate_map(current_catalog).get(proxmox_id)
            if active_claim is None:
                if current_candidate is None or not _candidate_is_actionable(
                    current_candidate
                ):
                    raise OperatorStop("candidate_no_longer_actionable")
                manifest_hash = (current_candidate.get("adoption_plan") or {}).get(
                    "manifest_sha256"
                )
            else:
                manifest_hash = active_claim.manifest_sha256
            if not isinstance(manifest_hash, str) or not re.fullmatch(
                r"[0-9a-f]{64}", manifest_hash
            ):
                raise OperatorStop("manifest_hash_unavailable")

            manifest = _manifest_for(current_candidate, active_claim)
            required_devices = _required_external_devices(manifest)
            missing_devices = sorted(
                device_id
                for device_id in required_devices
                if (proxmox_id, device_id) not in approved_ips
            )
            if missing_devices:
                outcomes.append(
                    _safe_result(
                        proxmox_id,
                        name,
                        "blocked",
                        details=[
                            f"authoritative_ip_missing:net{device_id}"
                            for device_id in missing_devices
                        ],
                    )
                )
                continue
            overrides = tuple(
                (device_id, approved_ips[(proxmox_id, device_id)])
                for device_id in sorted(required_devices)
            )
            cluster, vmid_text = proxmox_id.split(":", 1)
            target = Target(
                proxmox_id=proxmox_id,
                cluster=cluster,
                vmid=int(vmid_text),
                manifest_sha256=manifest_hash,
                network_ip_overrides=overrides,
            )
            result = run_one(
                target,
                timeout_seconds=timeout_seconds,
                poll_seconds=poll_seconds,
            )
            outcomes.append(
                _safe_result(
                    proxmox_id,
                    name,
                    "managed",
                    details={
                        "claim_id": result["claim"]["id"],
                        "execution_id": result["execution"]["id"],
                        "cloudstack_vm_ref": result["claim"][
                            "cloudstack_vm_ref"
                        ],
                        "cloudstack_instance_name": result["claim"][
                            "cloudstack_instance_name"
                        ],
                        "calls_this_run": result["calls_this_run"],
                    },
                )
            )
        except OperatorStop as exc:
            outcomes.append(
                _safe_result(proxmox_id, name, "failed_safe", details=str(exc))
            )
        except HTTPException as exc:
            outcomes.append(
                _safe_result(
                    proxmox_id,
                    name,
                    "failed_safe",
                    details=_safe_http_exception(exc),
                )
            )
        except Exception as exc:
            outcomes.append(
                _safe_result(
                    proxmox_id,
                    name,
                    "failed_safe",
                    details=type(exc).__name__,
                )
            )

    counts = {}
    for row in outcomes:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    return {
        "scope": "all_current_non_ceph_plus_active_claims",
        "total_accounted": len(outcomes),
        "counts": dict(sorted(counts.items())),
        "outcomes": outcomes,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Process every current non-Ceph adoption candidate."
    )
    parser.add_argument(
        "--approved-ip",
        action="append",
        default=[],
        metavar="cluster:vmid/netN=IPv4",
    )
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not 30 <= args.timeout <= 3600:
            raise OperatorStop("timeout_out_of_range")
        if not 0.5 <= args.poll_seconds <= 30:
            raise OperatorStop("poll_seconds_out_of_range")
        result = run_complete_queue(
            _parse_approved_ips(args.approved_ip),
            timeout_seconds=args.timeout,
            poll_seconds=args.poll_seconds,
        )
    except OperatorStop as exc:
        print(json.dumps({"status": "stopped", "code": str(exc)}, sort_keys=True))
        return 2
    except Exception as exc:
        print(
            json.dumps(
                {"status": "stopped", "code": type(exc).__name__},
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
