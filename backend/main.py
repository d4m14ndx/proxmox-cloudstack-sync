import logging
import json
import secrets
import threading
import ipaddress
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import load_settings
from database import (
    AdoptionClaim,
    AdoptionExecution,
    CloudStackVM,
    HostMapping,
    NetworkMapping,
    ProxmoxVM,
    SyncLog,
    get_session,
    init_db,
)
from sync_engine import SyncEngine
from adoption import (
    build_adoption_manifest,
    canonical_adoption_manifest_json,
    hash_adoption_manifest,
    select_exact_service_offering,
)
from adoption_registry import (
    ClaimConflict,
    ClaimInvalid,
    ClaimNotFound,
    acquire_managed_operation_lease,
    activate_bound_claim,
    bind_claim,
    bound_status_bindings,
    bound_status_map,
    complete_managed_operation_lease,
    finalize_retiring_claim,
    public_claim,
    reserve_claim,
    retire_claim,
    validated_claim_state,
)
from adoption_executor import (
    ExecutionConflict,
    ExecutionInvalid,
    authorize_cleanup_delete,
    create_execution,
    load_exact_external_vm,
    public_execution,
    _vm_matches_plan,
    reconcile_active_executions,
    reconcile_execution,
    request_execution_cleanup,
    request_execution_retry,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

settings = load_settings()
engine: SyncEngine | None = None
scheduler = BackgroundScheduler()
last_sync_result: dict = {}
sync_lock = threading.Lock()
last_adoption_executor_result: dict = {}


def _ip_in_guest_ranges(value: str, ranges: list[dict]) -> bool:
    try:
        candidate = ipaddress.ip_address(value)
        for ip_range in ranges:
            if not ip_range.get("startip") or not ip_range.get("endip"):
                continue
            start = ipaddress.ip_address(ip_range["startip"])
            end = ipaddress.ip_address(ip_range["endip"])
            if (
                start.version == candidate.version == end.version
                and int(start) <= int(candidate) <= int(end)
            ):
                return True
    except (ValueError, TypeError):
        return False
    return False


def _is_cloudstack_db_host_id(value: object) -> bool:
    """Return whether value is a canonical positive CloudStack DB host ID."""

    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        return False
    return value == str(int(value)) and int(value) > 0


def _is_exact_external_ipam_l2_network(
    network: dict,
    *,
    mapped_name: str,
    proxmox_vlan: int | None,
    host_zone_id: str | None,
    expected_domain_id: str | None,
) -> bool:
    """Validate the exact L2 network identity before bypassing CS-managed IPAM."""

    if (
        isinstance(proxmox_vlan, bool)
        or not isinstance(proxmox_vlan, int)
        or proxmox_vlan <= 0
    ):
        return False
    mapped = SyncEngine._canonical_mapping_value(mapped_name)
    observed = SyncEngine._canonical_mapping_value(network.get("name"))
    if (
        mapped is None
        or mapped != observed
        or network.get("type") != "L2"
        or network.get("broadcastdomaintype") != "Vlan"
        or network.get("vlan") != str(proxmox_vlan)
        or network.get("state") != "Setup"
        or network.get("canusefordeploy") is not True
        or network.get("account") != "admin"
        or network.get("domain") != "ROOT"
        or network.get("domainpath") != "ROOT"
        or not host_zone_id
        or network.get("zoneid") != host_zone_id
    ):
        return False
    return expected_domain_id is None or network.get("domainid") == expected_domain_id


def require_operator(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> None:
    expected = settings.api_auth_token
    if not expected:
        raise HTTPException(
            503,
            "Operator API is disabled until api_auth_token is configured",
        )
    provided = x_api_key or ""
    if authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(
            401,
            "Operator authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_adoption_registry(
    x_adoption_registry_token: str | None = Header(
        default=None,
        alias="X-Adoption-Registry-Token",
    ),
) -> None:
    """Authenticate a management-server extension to the claim registry."""

    if not settings.adoption_registry_enabled:
        raise HTTPException(503, "Adoption registry is disabled")
    expected = settings.adoption_registry_internal_token
    if (
        not expected
        or not x_adoption_registry_token
        or not secrets.compare_digest(x_adoption_registry_token, expected)
    ):
        raise HTTPException(401, "Adoption registry authentication required")


def run_sync():
    global last_sync_result
    if not sync_lock.acquire(blocking=False):
        return None
    try:
        last_sync_result = engine.full_sync()
        last_sync_result["timestamp"] = datetime.now(timezone.utc).isoformat()
    except Exception as e:
        log.error("Sync failed (%s)", type(e).__name__)
        last_sync_result = {
            "error": "Sync failed",
            "error_type": type(e).__name__,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        sync_lock.release()
    return last_sync_result


def _activate_execution_claim(claim_id: str, generation: int) -> None:
    try:
        activate_adoption_claim(
            claim_id,
            ActivateAdoptionClaimRequest(generation=generation),
            None,
        )
    except HTTPException as exc:
        raise ClaimConflict("execution activation is not yet verifiable") from exc


def run_adoption_executor():
    global last_adoption_executor_result
    if not settings.adoption_executor_enabled:
        return None
    if not engine or not engine.cs_client:
        last_adoption_executor_result = {
            "error": "CloudStack API is not configured",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return last_adoption_executor_result
    result = reconcile_active_executions(
        client=engine.cs_client,
        lease_seconds=settings.adoption_executor_lease_seconds,
        activate=_activate_execution_claim,
    )
    result["timestamp"] = datetime.now(timezone.utc).isoformat()
    last_adoption_executor_result = result
    return result


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine
    init_db(settings.database_url)
    engine = SyncEngine(settings)

    scheduler.add_job(run_sync, "interval", seconds=settings.sync_interval_seconds, id="sync_job")
    if settings.adoption_executor_enabled:
        scheduler.add_job(
            run_adoption_executor,
            "interval",
            seconds=settings.adoption_executor_interval_seconds,
            id="adoption_executor_job",
            max_instances=1,
        )
    scheduler.start()
    run_sync()
    if settings.adoption_executor_enabled:
        run_adoption_executor()
    log.info(f"Scheduler started, syncing every {settings.sync_interval_seconds}s")
    yield
    scheduler.shutdown()


app = FastAPI(title="Proxmox-CloudStack Sync", lifespan=lifespan)

frontend_dir = Path(__file__).parent.parent / "frontend"
if frontend_dir.exists():
    app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")


@app.get("/")
def index():
    index_file = frontend_dir / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return {"message": "Proxmox-CloudStack Sync API", "docs": "/docs"}


# --- Status endpoints ---

@app.get("/api/status")
async def get_status():
    return {
        "last_sync": last_sync_result,
        "sync_interval": settings.sync_interval_seconds,
        "proxmox_clusters": [c.name for c in settings.proxmox_clusters],
        "cloudstack_configured": bool(settings.cloudstack.api_key),
        "cloudstack_db_configured": engine.cs_db is not None if engine else False,
        "cloudstack_db_credentials_configured": bool(settings.cloudstack_db.password),
        "cloudstack_db_error": engine.cs_db_last_error if engine else None,
        "auto_reconcile": settings.auto_reconcile,
        "nic_sync_enabled": settings.nic_sync_enabled,
        "auto_reconcile_nics": settings.auto_reconcile_nics,
        "operator_auth_configured": bool(settings.api_auth_token),
        "adoption_executor_enabled": settings.adoption_executor_enabled,
        "adoption_executor": last_adoption_executor_result,
    }


@app.post("/api/sync")
def trigger_sync(_: None = Depends(require_operator)):
    result = run_sync()
    if result is None:
        raise HTTPException(409, "Sync already in progress")
    return result


# --- Proxmox VM endpoints ---

@app.get("/api/proxmox/vms")
def list_proxmox_vms(
    cluster: str | None = None,
    matched: bool | None = None,
    status: str | None = None,
    include_stale: bool = False,
    _: None = Depends(require_operator),
):
    session = get_session()
    try:
        q = session.query(ProxmoxVM)
        if not include_stale:
            q = q.filter(ProxmoxVM.current.is_(True))
        if cluster:
            q = q.filter(ProxmoxVM.cluster == cluster)
        if matched is not None:
            q = q.filter(ProxmoxVM.matched == matched)
        if status:
            q = q.filter(ProxmoxVM.status == status)
        vms = q.order_by(ProxmoxVM.name).all()
        return [_px_to_dict(v) for v in vms]
    finally:
        session.close()


@app.get("/api/proxmox/clusters")
def list_proxmox_clusters(_: None = Depends(require_operator)):
    session = get_session()
    try:
        rows = session.query(
            ProxmoxVM.cluster,
        ).filter(ProxmoxVM.current.is_(True)).distinct().all()
        clusters = []
        for (cluster_name,) in rows:
            base = session.query(ProxmoxVM).filter_by(
                cluster=cluster_name, current=True
            )
            count = base.count()
            matched_count = base.filter_by(matched=True).count()
            clusters.append({
                "name": cluster_name,
                "total_vms": count,
                "matched_vms": matched_count,
                "unmatched_vms": count - matched_count,
            })
        return clusters
    finally:
        session.close()


@app.get("/api/adoption/candidates")
def list_adoption_candidates(_: None = Depends(require_operator)):
    """Fail-closed, read-only adoption preflight for the current PX snapshot."""
    session = get_session()
    try:
        policy = settings.adoption_policy
        policy_blockers = []
        offerings = []
        cloudstack_networks = {}
        cloudstack_hosts = {}
        cloudstack_hosts_by_name = {}
        cloudstack_clusters = {}
        adoption_templates = []
        guest_ip_ranges = {}
        existing_cloudstack_macs = set()
        existing_cloudstack_ips = set()
        if not policy.enabled:
            policy_blockers.append("adoption_policy_not_enabled")
        if not engine or not engine.cs_client:
            policy_blockers.append("cloudstack_api_not_configured")
        else:
            try:
                if policy.enabled:
                    domains = engine.cs_client.list_domains(id=policy.domain_id)
                    if len(domains) != 1:
                        policy_blockers.append("root_domain_not_unique")
                    else:
                        domain = domains[0]
                        if (
                            domain.get("id") != policy.domain_id
                            or domain.get("name") != "ROOT"
                            or domain.get("path") != "ROOT"
                        ):
                            policy_blockers.append("root_domain_identity_mismatch")

                offerings = engine.cs_client.list_service_offerings()
                networks = engine.cs_client.list_networks()
                for network in networks:
                    network_id = network.get("id")
                    if isinstance(network_id, str) and network_id == network_id.strip():
                        cloudstack_networks.setdefault(network_id, []).append(network)

                for host in engine.cs_client.list_hosts(
                    hypervisor="External", details="all"
                ):
                    host_id = host.get("id")
                    if isinstance(host_id, str) and host_id == host_id.strip():
                        cloudstack_hosts.setdefault(host_id, []).append(host)
                    host_name = SyncEngine._canonical_mapping_value(host.get("name"))
                    if host_name is not None:
                        cloudstack_hosts_by_name.setdefault(host_name, []).append(host)

                if settings.adoption_executor_enabled:
                    for cluster in engine.cs_client.list_clusters():
                        cluster_id = cluster.get("id")
                        if isinstance(cluster_id, str) and cluster_id == cluster_id.strip():
                            cloudstack_clusters.setdefault(cluster_id, []).append(cluster)
                    adoption_templates = engine.cs_client.list_templates(
                        id=policy.template_id
                    )

                for ip_range in engine.cs_client.list_vlan_ip_ranges():
                    network_id = ip_range.get("networkid")
                    if isinstance(network_id, str) and network_id:
                        guest_ip_ranges.setdefault(network_id, []).append(ip_range)

                for vm in engine.cs_client.list_virtual_machines(details="all"):
                    for nic in vm.get("nic") or []:
                        mac = nic.get("macaddress")
                        if isinstance(mac, str) and mac:
                            existing_cloudstack_macs.add(mac.upper())
                        ip = nic.get("ipaddress")
                        if isinstance(ip, str) and ip:
                            existing_cloudstack_ips.add(ip)
            except Exception as e:
                log.error(
                    "Adoption catalog lookup failed (%s)", type(e).__name__
                )
                policy_blockers.append("adoption_catalog_lookup_failed")

        host_mappings = {
            (
                SyncEngine._canonical_mapping_value(m.proxmox_cluster),
                SyncEngine._canonical_mapping_value(m.proxmox_node),
            ): m
            for m in SyncEngine._globally_unique_host_mappings(session)
        }
        network_mappings = {}
        for mapping in session.query(NetworkMapping).all():
            cluster = SyncEngine._canonical_mapping_value(mapping.proxmox_cluster)
            bridge = SyncEngine._canonical_mapping_value(mapping.proxmox_bridge)
            network_id = mapping.cloudstack_network_id
            if (
                cluster is None
                or bridge is None
                or not isinstance(network_id, str)
                or not network_id
                or network_id != network_id.strip()
            ):
                continue
            network_mappings.setdefault(
                (cluster, bridge, mapping.proxmox_vlan), []
            ).append(mapping)
        rows = []
        inventory_collection_current = getattr(
            engine, "_inventory_collection_ready", False
        )
        nic_collection_current = getattr(
            engine, "_nic_collection_ready", False
        )
        for px in session.query(ProxmoxVM).filter_by(current=True).order_by(
            ProxmoxVM.cluster, ProxmoxVM.node, ProxmoxVM.vmid
        ).all():
            networks = _json_list(px.networks)
            storage = _json_list(px.storage)
            blockers = []
            network_plan = []
            host_plan = None
            offering_plan = None
            template_plan = None
            resolved_host_zone_id = None
            manifest = None
            manifest_json = None
            manifest_hash = None
            config_current = bool(nic_collection_current and px.config_current)
            if px.template:
                disposition = "excluded_template"
                blockers.append("proxmox_template")
            elif px.vm_type != "qemu":
                disposition = "inventory_only"
                blockers.append("stock_cloudstack_extension_is_qemu_only")
            elif px.matched:
                disposition = "existing_external"
            else:
                disposition = "blocked"
                placement = (
                    SyncEngine._canonical_mapping_value(px.cluster),
                    SyncEngine._canonical_mapping_value(px.node),
                )
                host_mapping = host_mappings.get(placement)
                if host_mapping is None:
                    blockers.append("host_mapping_missing")
                else:
                    mapped_host_id = SyncEngine._canonical_mapping_value(
                        host_mapping.cloudstack_host_id
                    )
                    target_hosts = cloudstack_hosts.get(mapped_host_id, [])
                    if not target_hosts and _is_cloudstack_db_host_id(mapped_host_id):
                        mapped_host_name = SyncEngine._canonical_mapping_value(
                            host_mapping.cloudstack_host_name
                        )
                        target_hosts = cloudstack_hosts_by_name.get(
                            mapped_host_name, []
                        )
                    if len(target_hosts) != 1:
                        blockers.append(
                            "cloudstack_host_missing"
                            if not target_hosts
                            else "cloudstack_host_ambiguous"
                        )
                    else:
                        target_host = target_hosts[0]
                        resolved_host_zone_id = SyncEngine._canonical_mapping_value(
                            target_host.get("zoneid")
                        )
                        expected_name = SyncEngine._canonical_mapping_value(
                            host_mapping.cloudstack_host_name
                        )
                        observed_name = SyncEngine._canonical_mapping_value(
                            target_host.get("name")
                        )
                        observed_id = SyncEngine._canonical_mapping_value(
                            target_host.get("id")
                        )
                        if (
                            expected_name != observed_name
                            or observed_id is None
                            or target_host.get("hypervisor") != "External"
                            or target_host.get("state") != "Up"
                            or target_host.get("resourcestate") != "Enabled"
                        ):
                            blockers.append(
                                "cloudstack_host_identity_or_state_mismatch"
                            )
                        else:
                            host_details = target_host.get("details")
                            if not isinstance(host_details, dict) or (
                                SyncEngine._canonical_mapping_value(
                                    host_details.get("proxmox_cluster")
                                )
                                != placement[0]
                                or str(
                                    host_details.get(
                                        "adoption_status_registry_required", ""
                                    )
                                ).strip().lower()
                                != "true"
                            ):
                                blockers.append(
                                    "cloudstack_host_adoption_status_registry_not_enabled"
                                )
                            else:
                                host_plan = {
                                    "id": observed_id,
                                    "name": host_mapping.cloudstack_host_name,
                                    "state": "Up",
                                    "resource_state": "Enabled",
                                    "proxmox_cluster": placement[0],
                                    "adoption_status_registry_required": True,
                                }
                                if settings.adoption_executor_enabled:
                                    zone_id = target_host.get("zoneid")
                                    cluster_id = target_host.get("clusterid")
                                    if not isinstance(zone_id, str) or not isinstance(
                                        cluster_id, str
                                    ):
                                        blockers.append(
                                            "cloudstack_host_zone_or_cluster_missing"
                                        )
                                    else:
                                        clusters = cloudstack_clusters.get(cluster_id, [])
                                        if len(clusters) != 1:
                                            blockers.append(
                                                "cloudstack_cluster_missing"
                                                if not clusters
                                                else "cloudstack_cluster_ambiguous"
                                            )
                                        else:
                                            cluster = clusters[0]
                                            extension_id = cluster.get("extensionid")
                                            if (
                                                cluster.get("hypervisortype") != "External"
                                                or cluster.get("zoneid") != zone_id
                                                or not isinstance(extension_id, str)
                                                or not extension_id
                                            ):
                                                blockers.append(
                                                    "cloudstack_cluster_extension_mismatch"
                                                )
                                            else:
                                                compatible_templates = [
                                                    template
                                                    for template in adoption_templates
                                                    if template.get("id") == policy.template_id
                                                    and template.get("hypervisor") == "External"
                                                    and template.get("extensionid") == extension_id
                                                    and template.get("isready") is True
                                                    and (
                                                        template.get("crosszones") is True or
                                                        template.get("zoneid") == zone_id
                                                        or any(
                                                            zone.get("id") == zone_id
                                                            for zone in template.get("zones") or []
                                                        )
                                                    )
                                                ]
                                                if len(compatible_templates) != 1:
                                                    blockers.append(
                                                        "adoption_template_not_ready_or_ambiguous"
                                                    )
                                                else:
                                                    host_plan.update(
                                                        {
                                                            "zone_id": zone_id,
                                                            "cluster_id": cluster_id,
                                                            "extension_id": extension_id,
                                                        }
                                                    )
                                                    template_plan = {
                                                        "id": policy.template_id,
                                                        "name": compatible_templates[0].get(
                                                            "name"
                                                        ),
                                                        "hypervisor": "External",
                                                        "extension_id": extension_id,
                                                        "zone_id": zone_id,
                                                        "ready": True,
                                                    }
                if not inventory_collection_current:
                    blockers.append("inventory_collection_not_current")
                if not config_current:
                    blockers.append("config_snapshot_not_current")
                else:
                    if not networks:
                        blockers.append("nic_inventory_missing")
                    seen_macs = set()
                    for nic in networks:
                        if not nic.get("mac") or not nic.get("bridge"):
                            blockers.append(
                                f"nic{nic.get('device_id', '?')}_identity_incomplete"
                            )
                            continue
                        mac = nic["mac"].upper()
                        if mac in seen_macs:
                            blockers.append("nic_mac_duplicate_within_vm")
                        seen_macs.add(mac)
                        if mac in existing_cloudstack_macs:
                            blockers.append(
                                f"nic{nic.get('device_id', '?')}_mac_already_in_cloudstack"
                            )
                        if not nic.get("ip"):
                            blockers.append(
                                f"nic{nic.get('device_id', '?')}_ip_unresolved"
                            )
                        elif nic["ip"] in existing_cloudstack_ips:
                            blockers.append(
                                f"nic{nic.get('device_id', '?')}_ip_already_in_cloudstack"
                            )
                        key = (
                            SyncEngine._canonical_mapping_value(px.cluster),
                            SyncEngine._canonical_mapping_value(nic["bridge"]),
                            nic.get("vlan"),
                        )
                        mapped = network_mappings.get(key, [])
                        if len(mapped) != 1:
                            blockers.append(
                                f"network_mapping_{'missing' if not mapped else 'ambiguous'}:"
                                f"{nic['bridge']}:"
                                f"{nic.get('vlan') if nic.get('vlan') is not None else 'untagged'}"
                            )
                            continue
                        mapping = mapped[0]
                        target = cloudstack_networks.get(
                            mapping.cloudstack_network_id, []
                        )
                        if len(target) != 1:
                            blockers.append(
                                f"cloudstack_network_{'missing' if not target else 'ambiguous'}:"
                                f"{mapping.cloudstack_network_id}"
                            )
                            continue
                        target_network = target[0]
                        ip_allocation = "cloudstack"
                        if target_network.get("type") == "L2":
                            if not _is_exact_external_ipam_l2_network(
                                target_network,
                                mapped_name=mapping.cloudstack_network_name,
                                proxmox_vlan=nic.get("vlan"),
                                host_zone_id=resolved_host_zone_id,
                                expected_domain_id=(
                                    policy.domain_id if policy.enabled else None
                                ),
                            ):
                                blockers.append(
                                    f"nic{nic.get('device_id', '?')}_"
                                    "l2_network_identity_mismatch"
                                )
                            else:
                                ip_allocation = "external"
                        elif nic.get("ip") and not _ip_in_guest_ranges(
                            nic["ip"],
                            guest_ip_ranges.get(
                                mapping.cloudstack_network_id, []
                            ),
                        ):
                            blockers.append(
                                f"nic{nic.get('device_id', '?')}_"
                                "ip_outside_cloudstack_range"
                            )
                        network_plan.append({
                            "device_id": nic.get("device_id"),
                            "mac": mac,
                            "ip": nic.get("ip"),
                            "netmask": nic.get("netmask"),
                            "gateway": nic.get("gateway"),
                            "proxmox_bridge": nic.get("bridge"),
                            "proxmox_vlan": nic.get("vlan"),
                            "cloudstack_network_id": mapping.cloudstack_network_id,
                            "cloudstack_network_name": target[0].get("name"),
                            "ip_allocation": ip_allocation,
                        })
                    data_disks = [
                        d for d in storage if d.get("media") != "cdrom"
                    ]
                    if not data_disks:
                        blockers.append("storage_inventory_missing")
                    elif any(
                        not d.get("device")
                        or not d.get("volume")
                        or not d.get("storage")
                        or not d.get("size")
                        for d in data_disks
                    ):
                        blockers.append("storage_identity_incomplete")
                    elif (
                        len({d["device"] for d in data_disks}) != len(data_disks)
                        or len({d["volume"] for d in data_disks}) != len(data_disks)
                    ):
                        blockers.append("storage_identity_duplicate")

                    if not policy_blockers:
                        offering_plan, offering_blockers = (
                            select_exact_service_offering(
                                px.cpus,
                                px.memory_mb,
                                offerings,
                                policy.customized_service_offering_id,
                                policy.customized_service_offering_cpu_speed_mhz,
                            )
                        )
                        blockers.extend(offering_blockers)
                    else:
                        blockers.extend(policy_blockers)

                    if not blockers:
                        manifest = build_adoption_manifest(
                            cluster=px.cluster,
                            node=px.node,
                            vmid=px.vmid,
                            name=px.name,
                            status=px.status,
                            cpus=px.cpus,
                            memory_mb=px.memory_mb,
                            networks=network_plan,
                            storage=data_disks,
                        )
                        manifest_json = canonical_adoption_manifest_json(manifest)
                        manifest_hash = hash_adoption_manifest(manifest)
                if not settings.adoption_executor_enabled:
                    blockers.append("adoption_executor_not_enabled")

            if disposition == "blocked" and not blockers:
                disposition = "ready"

            rows.append({
                "proxmox_id": px.id,
                "cluster": px.cluster,
                "node": px.node,
                "vmid": px.vmid,
                "name": px.name,
                "vm_type": px.vm_type,
                "template": px.template,
                "status": px.status,
                "config_current": config_current,
                "disposition": disposition,
                "matched": px.matched,
                "match_source": px.match_source,
                "cloudstack_uuid": px.cloudstack_uuid,
                "networks": networks,
                "storage": storage,
                "adoption_plan": {
                    "owner": {
                        "account": policy.account,
                        "domain_id": policy.domain_id or None,
                        "project_id": None,
                    },
                    "host": host_plan,
                    "service_offering": offering_plan,
                    "template": template_plan,
                    "networks": network_plan,
                    "manifest": manifest,
                    "extension_external_details": (
                        {
                            "adopt_existing": "true",
                            "adopt_manifest_sha256": manifest_hash,
                            "adopt_manifest_json": manifest_json,
                        }
                        if manifest_hash is not None
                        else None
                    ),
                    "manifest_sha256": manifest_hash,
                } if disposition in {"blocked", "ready"} else None,
                "blockers": sorted(set(blockers)),
            })
        summary = {
            "total_current": len(rows),
            "existing_external": sum(
                r["disposition"] == "existing_external" for r in rows
            ),
            "blocked_qemu": sum(r["disposition"] == "blocked" for r in rows),
            "inventory_only": sum(
                r["disposition"] == "inventory_only" for r in rows
            ),
            "templates": sum(
                r["disposition"] == "excluded_template" for r in rows
            ),
            "ready": sum(
                r["disposition"] == "ready" for r in rows
            ),
        }
        return {
            "summary": summary,
            "freshness": {
                "inventory_collection_current": inventory_collection_current,
                "nic_collection_current": nic_collection_current,
            },
            "policy": {
                "enabled": policy.enabled,
                "account": policy.account,
                "domain_id": policy.domain_id or None,
                "project_id": None,
                "customized_service_offering_id": (
                    policy.customized_service_offering_id or None
                ),
                "template_id": policy.template_id or None,
                "executor_enabled": settings.adoption_executor_enabled,
                "blockers": sorted(set(policy_blockers)),
            },
            "candidates": rows,
        }
    finally:
        session.close()


class ReserveAdoptionClaimRequest(BaseModel):
    proxmox_id: str
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BindAdoptionClaimRequest(BaseModel):
    generation: int = Field(gt=0, strict=True)
    proxmox_cluster: str = Field(min_length=1)
    proxmox_node: str = Field(min_length=1)
    proxmox_vmid: int = Field(gt=0, strict=True)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cloudstack_vm_ref: str = Field(min_length=1)
    cloudstack_instance_name: str = Field(min_length=1)


class ActivateAdoptionClaimRequest(BaseModel):
    generation: int = Field(gt=0, strict=True)


class ExecuteAdoptionClaimRequest(BaseModel):
    generation: int = Field(gt=0, strict=True)


class ManagedOperationLeaseRequest(BindAdoptionClaimRequest):
    action: str = Field(
        pattern=(
            r"^(console|start|stop|reboot|create_snapshot|restore_snapshot|delete_snapshot)$"
        )
    )


class CompleteManagedOperationLeaseRequest(ManagedOperationLeaseRequest):
    lease_id: str = Field(min_length=36, max_length=36)


@app.post("/api/adoption/claims")
def create_adoption_claim(
    req: ReserveAdoptionClaimRequest,
    _: None = Depends(require_operator),
):
    """Reserve one planned VMID without touching CloudStack or Proxmox."""

    if not settings.adoption_registry_enabled:
        raise HTTPException(503, "Adoption registry is disabled")

    plan = list_adoption_candidates(None)
    candidates = [
        item for item in plan["candidates"] if item["proxmox_id"] == req.proxmox_id
    ]
    if len(candidates) != 1:
        raise HTTPException(404, "Current adoption candidate is not unique")
    candidate = candidates[0]
    allowed_blockers = {"adoption_executor_not_enabled"}
    blockers = set(candidate.get("blockers") or [])
    adoption_plan = candidate.get("adoption_plan") or {}
    manifest = adoption_plan.get("manifest")
    actual_hash = adoption_plan.get("manifest_sha256")
    if blockers - allowed_blockers or not manifest or not actual_hash:
        raise HTTPException(
            409,
            {
                "message": "Candidate does not pass the current reservation gate",
                "blockers": sorted(blockers),
            },
        )
    if not secrets.compare_digest(req.manifest_sha256, actual_hash):
        raise HTTPException(409, "Candidate manifest changed before reservation")

    manifest_json = canonical_adoption_manifest_json(manifest)
    session = get_session()
    try:
        reservation = reserve_claim(
            session,
            proxmox_cluster=candidate["cluster"],
            proxmox_node=candidate["node"],
            proxmox_vmid=candidate["vmid"],
            manifest_json=manifest_json,
            manifest_sha256=actual_hash,
        )
        claim = public_claim(reservation.claim)
        return {
            "claim": claim,
            "extension_external_details": {
                "adopt_existing": "true",
                "adopt_claim_id": reservation.claim.id,
                "adopt_claim_generation": reservation.claim.generation,
                "adopt_manifest_sha256": actual_hash,
                "adopt_manifest_json": manifest_json,
                "proxmox_cluster": candidate["cluster"],
            },
            "executor_enabled": settings.adoption_executor_enabled,
        }
    except ClaimInvalid as exc:
        session.rollback()
        raise HTTPException(400, str(exc)) from exc
    except ClaimConflict as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc
    finally:
        session.close()


@app.get("/api/adoption/claims")
def list_adoption_claims(_: None = Depends(require_operator)):
    session = get_session()
    try:
        claims = session.query(AdoptionClaim).order_by(AdoptionClaim.created_at).all()
        return [public_claim(claim) for claim in claims]
    finally:
        session.close()


def _build_execution_plan(candidate: dict, claim: AdoptionClaim) -> dict:
    adoption_plan = candidate.get("adoption_plan") or {}
    manifest = adoption_plan.get("manifest") or {}
    host = adoption_plan.get("host") or {}
    template = adoption_plan.get("template") or {}
    offering = adoption_plan.get("service_offering") or {}
    networks = adoption_plan.get("networks") or []
    if (
        adoption_plan.get("manifest_sha256") != claim.manifest_sha256
        or canonical_adoption_manifest_json(manifest) != claim.manifest_json
    ):
        raise ExecutionConflict("candidate manifest changed after reservation")
    if not host or not template or not offering or not networks:
        raise ExecutionInvalid("candidate execution plan is incomplete")
    if any(
        isinstance(network.get("device_id"), bool)
        or not isinstance(network.get("device_id"), int)
        or network.get("device_id") < 0
        for network in networks
    ):
        raise ExecutionInvalid("candidate network device identity is invalid")
    ordered_networks = sorted(networks, key=lambda network: network["device_id"])
    if [network["device_id"] for network in ordered_networks] != list(
        range(len(ordered_networks))
    ):
        raise ExecutionInvalid("candidate network devices are not contiguous")
    return {
        "claim": {
            "id": claim.id,
            "generation": claim.generation,
            "manifest_sha256": claim.manifest_sha256,
        },
        "deployment": {
            "zone_id": host.get("zone_id"),
            "cluster_id": host.get("cluster_id"),
            "host_id": host.get("id"),
            "template_id": template.get("id"),
            "service_offering_id": offering.get("id"),
            "service_offering_customized": offering.get("customized"),
            "cpu_speed_mhz": (
                (offering.get("details") or {}).get("cpuSpeed")
                if offering.get("customized") is True
                else None
            ),
            "account": "admin",
            "domain_id": settings.adoption_policy.domain_id,
            "project_id": None,
            "name": f"adopt-{claim.proxmox_vmid}-{claim.id[:8]}",
            "display_name": manifest.get("name"),
            "cpus": manifest.get("cpus"),
            "memory_mib": manifest.get("memory_mib"),
            "networks": [
                {
                    "network_id": network.get("cloudstack_network_id"),
                    "mac": network.get("mac"),
                    "ip": network.get("ip"),
                    "ip_allocation": network.get(
                        "ip_allocation", "cloudstack"
                    ),
                    "device_id": network.get("device_id"),
                }
                for network in ordered_networks
            ],
            "external_details": {
                "adopt_existing": "true",
                "adopt_claim_id": claim.id,
                "adopt_claim_generation": str(claim.generation),
                "adopt_manifest_sha256": claim.manifest_sha256,
                "adopt_manifest_json": claim.manifest_json,
                "proxmox_cluster": claim.proxmox_cluster,
            },
        },
    }


@app.post("/api/adoption/claims/{claim_id}/execute", status_code=202)
def execute_adoption_claim(
    claim_id: str,
    req: ExecuteAdoptionClaimRequest,
    _: None = Depends(require_operator),
):
    """Create or resume one durable, deterministic adoption execution."""

    if not settings.adoption_executor_enabled:
        raise HTTPException(503, "Adoption executor is disabled")
    if not settings.adoption_registry_enabled:
        raise HTTPException(503, "Adoption registry is disabled")
    if not engine or not engine.cs_client:
        raise HTTPException(503, "CloudStack API is not configured")

    session = get_session()
    try:
        claim = session.query(AdoptionClaim).filter_by(id=claim_id).first()
        if claim is None:
            raise HTTPException(404, "Adoption claim not found")
        if claim.generation != req.generation:
            raise HTTPException(409, "Adoption claim generation changed")
        candidate_id = f"{claim.proxmox_cluster}:{claim.proxmox_vmid}"
        planning = list_adoption_candidates(None)
        candidates = [
            candidate
            for candidate in planning["candidates"]
            if candidate.get("proxmox_id") == candidate_id
        ]
        if len(candidates) != 1:
            raise HTTPException(409, "Current adoption candidate is not unique")
        candidate = candidates[0]
        blockers = sorted(set(candidate.get("blockers") or []))
        if blockers:
            raise HTTPException(
                409,
                {
                    "message": "Candidate does not pass the execution gate",
                    "blockers": blockers,
                },
            )
        execution = create_execution(
            session,
            claim_id=claim.id,
            generation=claim.generation,
            plan=_build_execution_plan(candidate, claim),
        )
        execution_id = execution.id
        response = public_execution(execution)
    except ExecutionInvalid as exc:
        session.rollback()
        raise HTTPException(400, str(exc)) from exc
    except ExecutionConflict as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc
    finally:
        session.close()

    advanced = reconcile_execution(
        execution_id,
        client=engine.cs_client,
        lease_seconds=settings.adoption_executor_lease_seconds,
        activate=_activate_execution_claim,
    )
    return advanced or response


@app.get("/api/adoption/executions")
def list_adoption_executions(_: None = Depends(require_operator)):
    session = get_session()
    try:
        executions = session.query(AdoptionExecution).order_by(
            AdoptionExecution.created_at
        ).all()
        return [public_execution(execution) for execution in executions]
    finally:
        session.close()


@app.get("/api/adoption/executions/{execution_id}")
def get_adoption_execution(
    execution_id: str,
    _: None = Depends(require_operator),
):
    session = get_session()
    try:
        execution = session.query(AdoptionExecution).filter_by(id=execution_id).first()
        if execution is None:
            raise HTTPException(404, "Adoption execution not found")
        return public_execution(execution)
    finally:
        session.close()


@app.post("/api/adoption/executions/{execution_id}/reconcile")
def reconcile_adoption_execution(
    execution_id: str,
    _: None = Depends(require_operator),
):
    if not settings.adoption_executor_enabled:
        raise HTTPException(503, "Adoption executor is disabled")
    if not engine or not engine.cs_client:
        raise HTTPException(503, "CloudStack API is not configured")
    result = reconcile_execution(
        execution_id,
        client=engine.cs_client,
        lease_seconds=settings.adoption_executor_lease_seconds,
        activate=_activate_execution_claim,
    )
    if result is not None:
        return result
    return get_adoption_execution(execution_id, None)


@app.post("/api/adoption/executions/{execution_id}/cleanup", status_code=202)
def cleanup_adoption_execution(
    execution_id: str,
    _: None = Depends(require_operator),
):
    if not settings.adoption_executor_enabled:
        raise HTTPException(503, "Adoption executor is disabled")
    if not engine or not engine.cs_client:
        raise HTTPException(503, "CloudStack API is not configured")
    try:
        return request_execution_cleanup(execution_id, client=engine.cs_client)
    except ExecutionInvalid as exc:
        raise HTTPException(400, str(exc)) from exc
    except ExecutionConflict as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/adoption/executions/{execution_id}/retry", status_code=202)
def retry_adoption_execution(
    execution_id: str,
    _: None = Depends(require_operator),
):
    if not settings.adoption_executor_enabled:
        raise HTTPException(503, "Adoption executor is disabled")
    if not engine or not engine.cs_client:
        raise HTTPException(503, "CloudStack API is not configured")
    try:
        request_execution_retry(execution_id, client=engine.cs_client)
        advanced = reconcile_execution(
            execution_id,
            client=engine.cs_client,
            lease_seconds=settings.adoption_executor_lease_seconds,
            activate=_activate_execution_claim,
        )
        return advanced or get_adoption_execution(execution_id, None)
    except ExecutionInvalid as exc:
        raise HTTPException(400, str(exc)) from exc
    except ExecutionConflict as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/internal/adoption/claims/{claim_id}/authorize-cleanup-delete")
def authorize_adoption_cleanup_delete(
    claim_id: str,
    req: BindAdoptionClaimRequest,
    _: None = Depends(require_adoption_registry),
):
    """Authorize one metadata-only delete during an explicit executor rollback."""

    session = get_session()
    try:
        execution = authorize_cleanup_delete(
            session,
            claim_id=claim_id,
            generation=req.generation,
            proxmox_cluster=req.proxmox_cluster,
            proxmox_node=req.proxmox_node,
            proxmox_vmid=req.proxmox_vmid,
            manifest_sha256=req.manifest_sha256,
            cloudstack_vm_ref=req.cloudstack_vm_ref,
            cloudstack_instance_name=req.cloudstack_instance_name,
        )
        return {"status": "cleanup_delete_authorized", "execution_id": execution.id}
    except ExecutionInvalid as exc:
        raise HTTPException(400, str(exc)) from exc
    except ExecutionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    finally:
        session.close()


@app.post("/api/internal/adoption/claims/{claim_id}/bind")
def bind_adoption_claim(
    claim_id: str,
    req: BindAdoptionClaimRequest,
    _: None = Depends(require_adoption_registry),
):
    """Atomically bind a reserved VMID to one CloudStack VM transaction."""

    session = get_session()
    try:
        claim = bind_claim(
            session,
            claim_id=claim_id,
            generation=req.generation,
            proxmox_cluster=req.proxmox_cluster,
            proxmox_node=req.proxmox_node,
            proxmox_vmid=req.proxmox_vmid,
            manifest_sha256=req.manifest_sha256,
            cloudstack_vm_ref=req.cloudstack_vm_ref,
            cloudstack_instance_name=req.cloudstack_instance_name,
        )
        return {"status": "bound", "claim": public_claim(claim)}
    except ClaimNotFound as exc:
        session.rollback()
        raise HTTPException(404, str(exc)) from exc
    except ClaimInvalid as exc:
        session.rollback()
        raise HTTPException(400, str(exc)) from exc
    except ClaimConflict as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc
    finally:
        session.close()


@app.post("/api/internal/adoption/claims/{claim_id}/lifecycle-state")
def adoption_claim_lifecycle_state(
    claim_id: str,
    req: BindAdoptionClaimRequest,
    _: None = Depends(require_adoption_registry),
):
    """Return state only after authenticating the complete lifecycle identity."""

    session = get_session()
    try:
        state = validated_claim_state(
            session,
            claim_id=claim_id,
            generation=req.generation,
            proxmox_cluster=req.proxmox_cluster,
            proxmox_node=req.proxmox_node,
            proxmox_vmid=req.proxmox_vmid,
            manifest_sha256=req.manifest_sha256,
            cloudstack_vm_ref=req.cloudstack_vm_ref,
            cloudstack_instance_name=req.cloudstack_instance_name,
        )
        return {"status": "ok", "state": state}
    except ClaimNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except ClaimInvalid as exc:
        raise HTTPException(400, str(exc)) from exc
    except ClaimConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    finally:
        session.close()


@app.post("/api/internal/adoption/claims/{claim_id}/lifecycle-lease")
def acquire_adoption_lifecycle_lease(
    claim_id: str,
    req: ManagedOperationLeaseRequest,
    _: None = Depends(require_adoption_registry),
):
    """Fence one exact managed mutation against concurrent retirement."""

    session = get_session()
    try:
        lease = acquire_managed_operation_lease(
            session,
            claim_id=claim_id,
            **req.model_dump(),
        )
        return {
            "status": "operating",
            "lease_id": lease.id,
            "action": lease.action,
            "expires_at": lease.expires_at.isoformat(),
        }
    except ClaimNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except ClaimInvalid as exc:
        session.rollback()
        raise HTTPException(400, str(exc)) from exc
    except ClaimConflict as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc
    finally:
        session.close()


@app.post("/api/internal/adoption/claims/{claim_id}/lifecycle-lease/complete")
def complete_adoption_lifecycle_lease(
    claim_id: str,
    req: CompleteManagedOperationLeaseRequest,
    _: None = Depends(require_adoption_registry),
):
    """Complete exactly one lease without clearing any newer fence."""

    session = get_session()
    try:
        state = complete_managed_operation_lease(
            session,
            claim_id=claim_id,
            **req.model_dump(),
        )
        return {"status": "ok", "state": state, "lease_id": req.lease_id}
    except ClaimNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except ClaimInvalid as exc:
        session.rollback()
        raise HTTPException(400, str(exc)) from exc
    except ClaimConflict as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc
    finally:
        session.close()


@app.get("/api/internal/adoption/status-map")
def adoption_status_map(
    proxmox_cluster: str = Query(min_length=1),
    _: None = Depends(require_adoption_registry),
):
    """Map cluster-local VMID to durable CloudStack instance identity."""

    session = get_session()
    try:
        try:
            mapping = bound_status_map(
                session,
                proxmox_cluster=proxmox_cluster,
            )
            bindings = bound_status_bindings(
                session,
                proxmox_cluster=proxmox_cluster,
            )
        except ClaimInvalid as exc:
            raise HTTPException(400, str(exc)) from exc
        return {
            "proxmox_cluster": proxmox_cluster,
            "vmid_to_instance_name": mapping,
            "bindings": bindings,
        }
    finally:
        session.close()


def _cloudstack_activation_mismatches(
    session,
    claim: AdoptionClaim,
    cloudstack_vm: dict,
) -> list[str]:
    """Compare a bound claim with the authoritative CloudStack API row."""

    mismatches = []
    try:
        manifest = json.loads(claim.manifest_json)
        expected_cpus = int(manifest["cpus"])
        expected_memory = int(manifest["memory_mib"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return ["claim_manifest_invalid"]

    if cloudstack_vm.get("id") != claim.cloudstack_vm_ref:
        mismatches.append("cloudstack_vm_uuid_mismatch")
    if cloudstack_vm.get("instancename") != claim.cloudstack_instance_name:
        mismatches.append("cloudstack_instance_name_mismatch")
    if cloudstack_vm.get("hypervisor") != "External":
        mismatches.append("cloudstack_hypervisor_mismatch")
    if cloudstack_vm.get("state") != "Running":
        mismatches.append("cloudstack_vm_not_running")
    if SyncEngine._cloudstack_proxmox_vmid(cloudstack_vm) != claim.proxmox_vmid:
        mismatches.append("cloudstack_proxmox_vmid_mismatch")

    actual_cpus_value = cloudstack_vm.get("cpunumber")
    try:
        actual_cpus = (
            int(actual_cpus_value)
            if isinstance(actual_cpus_value, (int, str))
            else None
        )
    except ValueError:
        actual_cpus = None
    actual_memory_value = cloudstack_vm.get("memory")
    try:
        actual_memory = (
            int(actual_memory_value)
            if isinstance(actual_memory_value, (int, str))
            else None
        )
    except ValueError:
        actual_memory = None
    if actual_cpus != expected_cpus:
        mismatches.append("cloudstack_cpu_mismatch")
    if actual_memory != expected_memory:
        mismatches.append("cloudstack_memory_mismatch")

    policy = settings.adoption_policy
    if cloudstack_vm.get("account") != policy.account:
        mismatches.append("cloudstack_account_mismatch")
    if cloudstack_vm.get("domainid") != policy.domain_id:
        mismatches.append("cloudstack_domain_mismatch")
    if cloudstack_vm.get("projectid"):
        mismatches.append("cloudstack_project_present")

    cluster = SyncEngine._canonical_mapping_value(claim.proxmox_cluster)
    node = SyncEngine._canonical_mapping_value(claim.proxmox_node)
    mappings = [
        mapping
        for mapping in SyncEngine._globally_unique_host_mappings(session)
        if (
            SyncEngine._canonical_mapping_value(mapping.proxmox_cluster),
            SyncEngine._canonical_mapping_value(mapping.proxmox_node),
        )
        == (cluster, node)
    ]
    if len(mappings) != 1:
        mismatches.append("cloudstack_host_mapping_not_unique")
    else:
        mapped_host_id = SyncEngine._canonical_mapping_value(
            mappings[0].cloudstack_host_id
        )
        actual_host_id = SyncEngine._canonical_mapping_value(
            cloudstack_vm.get("hostid")
        )
        mapped_host_name = SyncEngine._canonical_mapping_value(
            mappings[0].cloudstack_host_name
        )
        actual_host_name = SyncEngine._canonical_mapping_value(
            cloudstack_vm.get("hostname")
        )
        if (
            mapped_host_name != actual_host_name
            or (
                mapped_host_id != actual_host_id
                and not _is_cloudstack_db_host_id(mapped_host_id)
            )
        ):
            mismatches.append("cloudstack_host_mismatch")

    execution = session.query(AdoptionExecution).filter_by(
        claim_id=claim.id,
        generation=claim.generation,
    ).first()
    if execution is not None:
        if execution.state != "verifying":
            mismatches.append("adoption_execution_not_ready")
        else:
            try:
                execution_plan = json.loads(execution.plan_json)
            except (TypeError, json.JSONDecodeError):
                mismatches.append("adoption_execution_plan_invalid")
            else:
                if not _vm_matches_plan(cloudstack_vm, execution, execution_plan):
                    mismatches.append("cloudstack_execution_plan_mismatch")

    return sorted(set(mismatches))


@app.post("/api/adoption/claims/{claim_id}/activate")
def activate_adoption_claim(
    claim_id: str,
    req: ActivateAdoptionClaimRequest,
    _: None = Depends(require_operator),
):
    """Enable managed lifecycle after exact CloudStack deployment verification."""

    if not settings.adoption_registry_enabled:
        raise HTTPException(503, "Adoption registry is disabled")
    if not settings.adoption_policy.enabled:
        raise HTTPException(503, "Adoption policy is disabled")
    if engine is None or engine.cs_client is None:
        raise HTTPException(503, "CloudStack API is not configured")

    session = get_session()
    try:
        claim = session.query(AdoptionClaim).filter_by(id=claim_id).first()
        if claim is None:
            raise HTTPException(404, "claim does not exist")
        if claim.generation != req.generation:
            raise HTTPException(409, "claim generation is stale")
        if claim.state == "managed":
            return {"status": "managed", "claim": public_claim(claim)}
        if claim.state != "bound" or not claim.cloudstack_vm_ref:
            raise HTTPException(409, "claim is not ready for activation")

        try:
            cloudstack_rows = engine.cs_client.list_virtual_machines(
                id=claim.cloudstack_vm_ref,
                details="all",
            )
        except Exception as exc:
            log.error(
                "CloudStack activation verification failed (%s)",
                type(exc).__name__,
            )
            raise HTTPException(
                503, "CloudStack activation verification failed"
            ) from exc
        if len(cloudstack_rows) != 1:
            raise HTTPException(
                409, "CloudStack VM is not uniquely present for activation"
            )
        mismatches = _cloudstack_activation_mismatches(
            session, claim, cloudstack_rows[0]
        )
        if mismatches:
            raise HTTPException(
                409,
                {
                    "message": "CloudStack VM does not match the bound adoption",
                    "mismatches": mismatches,
                },
            )

        managed = activate_bound_claim(
            session,
            claim_id=claim.id,
            generation=req.generation,
            cloudstack_vm_ref=claim.cloudstack_vm_ref,
        )
        return {"status": "managed", "claim": public_claim(managed)}
    except ClaimNotFound as exc:
        session.rollback()
        raise HTTPException(404, str(exc)) from exc
    except ClaimInvalid as exc:
        session.rollback()
        raise HTTPException(400, str(exc)) from exc
    except ClaimConflict as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc
    finally:
        session.close()


@app.post("/api/internal/adoption/claims/{claim_id}/retire")
def retire_adoption_claim(
    claim_id: str,
    req: BindAdoptionClaimRequest,
    _: None = Depends(require_adoption_registry),
):
    """Tombstone identity before CloudStack metadata deletion is committed."""

    session = get_session()
    try:
        claim = retire_claim(
            session,
            claim_id=claim_id,
            generation=req.generation,
            proxmox_cluster=req.proxmox_cluster,
            proxmox_node=req.proxmox_node,
            proxmox_vmid=req.proxmox_vmid,
            manifest_sha256=req.manifest_sha256,
            cloudstack_vm_ref=req.cloudstack_vm_ref,
            cloudstack_instance_name=req.cloudstack_instance_name,
        )
        return {"status": "retiring", "claim": public_claim(claim)}
    except ClaimNotFound as exc:
        session.rollback()
        raise HTTPException(404, str(exc)) from exc
    except ClaimInvalid as exc:
        session.rollback()
        raise HTTPException(400, str(exc)) from exc
    except ClaimConflict as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc
    finally:
        session.close()


@app.post("/api/adoption/claims/{claim_id}/finalize-release")
def finalize_adoption_claim_release(
    claim_id: str,
    _: None = Depends(require_operator),
):
    """Release a tombstone only after CloudStack proves its VM UUID is absent."""

    if not settings.adoption_registry_enabled:
        raise HTTPException(503, "Adoption registry is disabled")
    if engine is None or engine.cs_client is None:
        raise HTTPException(503, "CloudStack API is not configured")

    session = get_session()
    try:
        claim = session.query(AdoptionClaim).filter_by(id=claim_id).first()
        if claim is None:
            raise HTTPException(404, "claim does not exist")
        if claim.state == "released":
            return {"status": "released", "claim": public_claim(claim)}
        if claim.state != "retiring" or not claim.cloudstack_vm_ref:
            raise HTTPException(409, "claim is not a finalizable tombstone")

        try:
            cloudstack_rows = load_exact_external_vm(
                engine.cs_client,
                claim.cloudstack_vm_ref,
            )
        except Exception as exc:
            log.error(
                "CloudStack absence verification failed (%s)",
                type(exc).__name__,
            )
            raise HTTPException(
                503, "CloudStack absence verification failed"
            ) from exc
        if cloudstack_rows:
            raise HTTPException(
                409,
                "CloudStack VM still exists; tombstone cannot be released",
            )

        finalized = finalize_retiring_claim(
            session,
            claim_id=claim.id,
            cloudstack_vm_ref=claim.cloudstack_vm_ref,
        )
        return {"status": "released", "claim": public_claim(finalized)}
    except ClaimNotFound as exc:
        session.rollback()
        raise HTTPException(404, str(exc)) from exc
    except ClaimInvalid as exc:
        session.rollback()
        raise HTTPException(400, str(exc)) from exc
    except ClaimConflict as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc
    finally:
        session.close()


# --- CloudStack VM endpoints ---

@app.get("/api/cloudstack/vms")
def list_cloudstack_vms(
    matched: bool | None = None,
    hypervisor: str | None = None,
    include_stale: bool = False,
    _: None = Depends(require_operator),
):
    session = get_session()
    try:
        q = session.query(CloudStackVM)
        if not include_stale:
            q = q.filter(CloudStackVM.current.is_(True))
        if matched is not None:
            q = q.filter(CloudStackVM.matched == matched)
        if hypervisor:
            q = q.filter(CloudStackVM.hypervisor == hypervisor)
        vms = q.order_by(CloudStackVM.name).all()
        return [_cs_to_dict(v) for v in vms]
    finally:
        session.close()


@app.get("/api/cloudstack/hosts")
def list_cloudstack_hosts(_: None = Depends(require_operator)):
    if not engine.cs_client:
        raise HTTPException(400, "CloudStack not configured")
    return engine.cs_client.list_hosts()


@app.get("/api/cloudstack/clusters")
def list_cs_clusters(_: None = Depends(require_operator)):
    if not engine.cs_client:
        raise HTTPException(400, "CloudStack not configured")
    return engine.cs_client.list_clusters()


@app.get("/api/cloudstack/zones")
def list_cs_zones(_: None = Depends(require_operator)):
    if not engine.cs_client:
        raise HTTPException(400, "CloudStack not configured")
    return engine.cs_client.list_zones()


@app.get("/api/cloudstack/service-offerings")
def list_service_offerings(_: None = Depends(require_operator)):
    if not engine.cs_client:
        raise HTTPException(400, "CloudStack not configured")
    return engine.cs_client.list_service_offerings()


@app.get("/api/cloudstack/networks")
def list_cs_networks(_: None = Depends(require_operator)):
    if not engine.cs_client:
        raise HTTPException(400, "CloudStack not configured")
    return engine.cs_client.list_networks()


@app.get("/api/cloudstack/disk-offerings")
def list_cs_disk_offerings(_: None = Depends(require_operator)):
    if not engine.cs_client:
        raise HTTPException(400, "CloudStack not configured")
    return engine.cs_client.list_disk_offerings()


# --- Drift detection ---

@app.get("/api/drift")
def get_drift(_: None = Depends(require_operator)):
    return engine.detect_drift()


# --- Matching ---

class MatchRequest(BaseModel):
    proxmox_id: str
    cloudstack_uuid: str


@app.post("/api/match")
def manual_match(req: MatchRequest, _: None = Depends(require_operator)):
    session = get_session()
    try:
        px = session.query(ProxmoxVM).filter_by(id=req.proxmox_id).first()
        cs = session.query(CloudStackVM).filter_by(uuid=req.cloudstack_uuid).first()
        if not px:
            raise HTTPException(404, f"Proxmox VM {req.proxmox_id} not found")
        if not cs:
            raise HTTPException(404, f"CloudStack VM {req.cloudstack_uuid} not found")
        if not px.current or not cs.current:
            raise HTTPException(409, "Only records from the current sync may be matched")
        if px.vm_type != "qemu" or px.template:
            raise HTTPException(409, "Only current non-template QEMU guests may be matched")
        if cs.hypervisor != "External":
            raise HTTPException(409, "Only CloudStack External VMs may be matched")
        if cs.proxmox_vmid is None:
            raise HTTPException(
                409,
                "CloudStack External VMID is required for a manual match",
            )
        if cs.proxmox_vmid != px.vmid:
            raise HTTPException(409, "CloudStack External VMID does not match Proxmox VMID")
        mapping = SyncEngine._unique_host_mapping(
            session, px.cluster, px.node, cs.host_name
        )
        if not mapping:
            raise HTTPException(
                409,
                "CloudStack host mapping does not uniquely match the Proxmox placement",
            )
        if px.matched and px.cloudstack_uuid != cs.uuid:
            raise HTTPException(409, "Proxmox VM is already matched; unmatch it first")
        if cs.matched and cs.proxmox_id != px.id:
            raise HTTPException(409, "CloudStack VM is already matched; unmatch it first")

        px.matched = True
        px.cloudstack_uuid = cs.uuid
        px.match_source = "manual"
        cs.matched = True
        cs.proxmox_id = px.id
        cs.match_source = "manual"
        session.commit()

        engine._log(session, "manual_match",
                    f"Matched {px.name} ({px.id}) <-> {cs.name} ({cs.uuid})")
        session.commit()
        return {"status": "matched", "proxmox": _px_to_dict(px), "cloudstack": _cs_to_dict(cs)}
    finally:
        session.close()


@app.post("/api/unmatch/{proxmox_id}")
def unmatch_vm(proxmox_id: str, _: None = Depends(require_operator)):
    session = get_session()
    try:
        px = session.query(ProxmoxVM).filter_by(id=proxmox_id).first()
        if not px:
            raise HTTPException(404, "VM not found")
        if px.cloudstack_uuid:
            cs = session.query(CloudStackVM).filter_by(uuid=px.cloudstack_uuid).first()
            if cs:
                cs.matched = False
                cs.proxmox_id = None
                cs.match_source = ""
        px.matched = False
        px.cloudstack_uuid = None
        px.match_source = ""
        session.commit()
        return {"status": "unmatched"}
    finally:
        session.close()


# --- Permanently unavailable legacy registration / repair ---

class RegisterRequest(BaseModel):
    proxmox_id: str
    service_offering_id: int
    account_id: int
    domain_id: int
    guest_os_id: int = 1


@app.post("/api/register")
def register_vm(_req: RegisterRequest, _: None = Depends(require_operator)):
    """Reject unsupported direct-DB VM registration unconditionally."""
    raise HTTPException(
        410,
        "Direct-DB registration has been removed; reviewed adopt-existing orchestration is required",
    )


@app.post("/api/cloudstack/repair-vm/{uuid}")
def removed_generic_repair(uuid: str, _: None = Depends(require_operator)):
    """Reject the unsafe generic direct-DB repair workflow unconditionally."""
    raise HTTPException(
        410,
        "Generic direct-DB repair has been removed; use a separately reviewed targeted workflow",
    )


@app.get("/api/cloudstack/db-hosts")
def list_db_hosts(_: None = Depends(require_operator)):
    """List hosts from CloudStack DB (includes zone/cluster context for registration)."""
    if not engine.cs_db:
        raise HTTPException(400, "CloudStack DB not configured")
    return engine.cs_db.list_hosts()


@app.get("/api/cloudstack/db-accounts")
def list_db_accounts(_: None = Depends(require_operator)):
    """List accounts from CloudStack DB for registration."""
    if engine is None or engine.cs_db is None:
        raise HTTPException(400, "CloudStack DB not configured")
    cs_db = engine.cs_db
    try:
        with cs_db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT a.id, a.uuid, a.account_name, a.domain_id, a.type, "
                    "d.name as domain_name "
                    "FROM account a JOIN domain d ON a.domain_id = d.id "
                    "WHERE a.removed IS NULL AND a.state = 'enabled' "
                    "ORDER BY d.name, a.account_name"
                )
                return cur.fetchall()
    except Exception as e:
        log.error("CloudStack account query failed (%s)", type(e).__name__)
        raise HTTPException(500, "CloudStack DB query failed")


@app.get("/api/cloudstack/db-service-offerings")
def list_db_service_offerings(_: None = Depends(require_operator)):
    """List service offerings from CloudStack DB for registration."""
    if engine is None or engine.cs_db is None:
        raise HTTPException(400, "CloudStack DB not configured")
    cs_db = engine.cs_db
    try:
        with cs_db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT so.id, dr.uuid, dr.name, so.cpu, so.ram_size "
                    "FROM service_offering so "
                    "JOIN disk_offering dr ON so.id = dr.id "
                    "WHERE dr.removed IS NULL "
                    "ORDER BY dr.name"
                )
                return cur.fetchall()
    except Exception as e:
        log.error(
            "CloudStack service-offering query failed (%s)",
            type(e).__name__,
        )
        raise HTTPException(500, "CloudStack DB query failed")


@app.get("/api/cloudstack/db-guest-os")
def list_db_guest_os(_: None = Depends(require_operator)):
    """List guest OS types from CloudStack DB."""
    if engine is None or engine.cs_db is None:
        raise HTTPException(400, "CloudStack DB not configured")
    cs_db = engine.cs_db
    try:
        with cs_db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, uuid, display_name FROM guest_os "
                    "WHERE removed IS NULL ORDER BY display_name LIMIT 200"
                )
                return cur.fetchall()
    except Exception as e:
        log.error("CloudStack guest-OS query failed (%s)", type(e).__name__)
        raise HTTPException(500, "CloudStack DB query failed")


# --- Host mappings ---

class HostMappingRequest(BaseModel):
    proxmox_cluster: str
    proxmox_node: str
    cloudstack_host_id: str
    cloudstack_host_name: str


@app.get("/api/host-mappings")
def list_host_mappings(_: None = Depends(require_operator)):
    session = get_session()
    try:
        mappings = session.query(HostMapping).order_by(
            HostMapping.proxmox_cluster, HostMapping.proxmox_node
        ).all()
        return [
            {
                "id": m.id,
                "proxmox_cluster": m.proxmox_cluster,
                "proxmox_node": m.proxmox_node,
                "cloudstack_host_id": m.cloudstack_host_id,
                "cloudstack_host_name": m.cloudstack_host_name,
            }
            for m in mappings
        ]
    finally:
        session.close()


@app.post("/api/host-mappings")
def create_host_mapping(req: HostMappingRequest, _: None = Depends(require_operator)):
    fields = (
        req.proxmox_cluster,
        req.proxmox_node,
        req.cloudstack_host_id,
        req.cloudstack_host_name,
    )
    if any(not value or value != value.strip() for value in fields):
        raise HTTPException(422, "Host mapping fields must be nonempty and normalized")

    session = get_session()
    try:
        cluster = SyncEngine._canonical_mapping_value(req.proxmox_cluster)
        node = SyncEngine._canonical_mapping_value(req.proxmox_node)
        host_name = SyncEngine._canonical_mapping_value(req.cloudstack_host_name)
        host_id = SyncEngine._canonical_mapping_value(req.cloudstack_host_id)
        rows = session.query(HostMapping).all()
        px_rows = [
            mapping for mapping in rows
            if (
                SyncEngine._canonical_mapping_value(mapping.proxmox_cluster)
                == cluster
                and SyncEngine._canonical_mapping_value(mapping.proxmox_node)
                == node
            )
        ]
        if len(px_rows) > 1:
            raise HTTPException(
                409, "Proxmox placement has conflicting host mappings"
            )
        existing = px_rows[0] if px_rows else None
        for mapping in rows:
            if existing is not None and mapping.id == existing.id:
                continue
            same_cs = (
                SyncEngine._canonical_mapping_value(mapping.cloudstack_host_id)
                == host_id
                or SyncEngine._canonical_mapping_value(mapping.cloudstack_host_name)
                == host_name
            )
            if same_cs:
                raise HTTPException(
                    409,
                    "CloudStack host is already mapped to another Proxmox placement",
                )
        if existing:
            existing_host = SyncEngine._canonical_mapping_value(
                existing.cloudstack_host_name
            )
            existing_host_id = SyncEngine._canonical_mapping_value(
                existing.cloudstack_host_id
            )
            if existing_host != host_name or existing_host_id != host_id:
                raise HTTPException(
                    409,
                    "Proxmox placement is already mapped; delete it before remapping",
                )
            existing.proxmox_cluster = req.proxmox_cluster
            existing.proxmox_node = req.proxmox_node
            existing.cloudstack_host_id = req.cloudstack_host_id
            existing.cloudstack_host_name = req.cloudstack_host_name
            session.commit()
            return {"status": "updated", "id": existing.id}

        mapping = HostMapping(
            proxmox_cluster=req.proxmox_cluster,
            proxmox_node=req.proxmox_node,
            cloudstack_host_id=req.cloudstack_host_id,
            cloudstack_host_name=req.cloudstack_host_name,
        )
        session.add(mapping)
        session.commit()

        engine._log(session, "host_mapping",
                    f"Mapped {req.proxmox_cluster}/{req.proxmox_node} -> "
                    f"{req.cloudstack_host_name} ({req.cloudstack_host_id})")
        session.commit()
        return {"status": "created", "id": mapping.id}
    finally:
        session.close()


@app.delete("/api/host-mappings/{mapping_id}")
def delete_host_mapping(mapping_id: int, _: None = Depends(require_operator)):
    session = get_session()
    try:
        mapping = session.query(HostMapping).filter_by(id=mapping_id).first()
        if not mapping:
            raise HTTPException(404, "Mapping not found")
        session.delete(mapping)
        session.commit()
        return {"status": "deleted"}
    finally:
        session.close()


@app.get("/api/host-mappings/proxmox-nodes")
def list_proxmox_nodes(_: None = Depends(require_operator)):
    """List unique proxmox cluster/node pairs from discovered VMs."""
    session = get_session()
    try:
        rows = session.query(
            ProxmoxVM.cluster, ProxmoxVM.node
        ).filter(ProxmoxVM.current.is_(True)).distinct().order_by(
            ProxmoxVM.cluster, ProxmoxVM.node
        ).all()
        return [{"cluster": r[0], "node": r[1]} for r in rows]
    finally:
        session.close()


# --- Network mappings ---

class NetworkMappingRequest(BaseModel):
    proxmox_cluster: str
    proxmox_bridge: str
    proxmox_vlan: int | None = None
    cloudstack_network_id: str
    cloudstack_network_name: str


@app.get("/api/network-mappings")
def list_network_mappings(_: None = Depends(require_operator)):
    session = get_session()
    try:
        mappings = session.query(NetworkMapping).order_by(
            NetworkMapping.proxmox_cluster, NetworkMapping.proxmox_bridge
        ).all()
        return [
            {
                "id": m.id,
                "proxmox_cluster": m.proxmox_cluster,
                "proxmox_bridge": m.proxmox_bridge,
                "proxmox_vlan": m.proxmox_vlan,
                "cloudstack_network_id": m.cloudstack_network_id,
                "cloudstack_network_name": m.cloudstack_network_name,
            }
            for m in mappings
        ]
    finally:
        session.close()


@app.post("/api/network-mappings")
def create_network_mapping(req: NetworkMappingRequest, _: None = Depends(require_operator)):
    session = get_session()
    try:
        existing = session.query(NetworkMapping).filter_by(
            proxmox_cluster=req.proxmox_cluster,
            proxmox_bridge=req.proxmox_bridge,
            proxmox_vlan=req.proxmox_vlan,
        ).first()
        if existing:
            existing.cloudstack_network_id = req.cloudstack_network_id
            existing.cloudstack_network_name = req.cloudstack_network_name
            session.commit()
            return {"status": "updated", "id": existing.id}

        mapping = NetworkMapping(
            proxmox_cluster=req.proxmox_cluster,
            proxmox_bridge=req.proxmox_bridge,
            proxmox_vlan=req.proxmox_vlan,
            cloudstack_network_id=req.cloudstack_network_id,
            cloudstack_network_name=req.cloudstack_network_name,
        )
        session.add(mapping)
        session.commit()

        vlan_desc = f" (VLAN {req.proxmox_vlan})" if req.proxmox_vlan else ""
        engine._log(session, "network_mapping",
                    f"Mapped {req.proxmox_cluster}/{req.proxmox_bridge}{vlan_desc} -> "
                    f"{req.cloudstack_network_name}")
        session.commit()
        return {"status": "created", "id": mapping.id}
    finally:
        session.close()


@app.delete("/api/network-mappings/{mapping_id}")
def delete_network_mapping(mapping_id: int, _: None = Depends(require_operator)):
    session = get_session()
    try:
        mapping = session.query(NetworkMapping).filter_by(id=mapping_id).first()
        if not mapping:
            raise HTTPException(404, "Mapping not found")
        session.delete(mapping)
        session.commit()
        return {"status": "deleted"}
    finally:
        session.close()


@app.get("/api/network-mappings/proxmox-bridges")
def list_proxmox_bridges(_: None = Depends(require_operator)):
    """Distinct (cluster, bridge, vlan) triples discovered in synced Proxmox NICs."""
    if not getattr(engine, "_nic_collection_ready", False):
        return []
    import json as _json
    session = get_session()
    try:
        seen = {}
        for px in session.query(ProxmoxVM).filter_by(current=True).all():
            if not px.networks:
                continue
            try:
                for n in _json.loads(px.networks):
                    bridge = n.get("bridge")
                    if not bridge:
                        continue
                    key = (px.cluster, bridge, n.get("vlan"))
                    seen[key] = {"cluster": px.cluster, "bridge": bridge,
                                 "vlan": n.get("vlan")}
            except Exception:
                continue
        return sorted(seen.values(), key=lambda x: (x["cluster"], x["bridge"],
                                                    x["vlan"] or 0))
    finally:
        session.close()


@app.get("/api/cloudstack/db-networks")
def list_db_networks(_: None = Depends(require_operator)):
    """List networks from the CloudStack DB (for network mapping)."""
    if not engine.cs_db:
        raise HTTPException(400, "CloudStack DB not configured")
    return engine.cs_db.list_networks()


# --- NICs ---

@app.get("/api/nics")
def list_nics(_: None = Depends(require_operator)):
    """Per-matched-VM side-by-side Proxmox vs CloudStack NIC comparison."""
    return engine.nic_comparison()


@app.get("/api/nics/drift")
def get_nic_drift(_: None = Depends(require_operator)):
    return engine.detect_nic_drift()


class ReconcileNicRequest(BaseModel):
    drift_item: dict
    dry_run: bool = False


@app.post("/api/reconcile/nic")
def reconcile_nic(req: ReconcileNicRequest, _: None = Depends(require_operator)):
    if not engine.cs_db:
        raise HTTPException(400, "CloudStack DB not configured")
    return engine.reconcile_nic(req.drift_item, dry_run=req.dry_run)


@app.post("/api/reconcile/nics-all")
def reconcile_nics_all(
    dry_run: bool = False,
    _: None = Depends(require_operator),
):
    return engine.reconcile_nics_all(dry_run=dry_run)


# --- Reconciliation ---

class ReconcileVmRequest(BaseModel):
    drift_item: dict


@app.post("/api/reconcile/vm")
def reconcile_vm(req: ReconcileVmRequest, _: None = Depends(require_operator)):
    if not engine.cs_db:
        raise HTTPException(400, "CloudStack DB not configured")
    return engine.reconcile_vm(req.drift_item)


@app.post("/api/reconcile/all")
def reconcile_all(_: None = Depends(require_operator)):
    return engine.reconcile_all()


@app.get("/api/reconcile/status")
def reconcile_status(_: None = Depends(require_operator)):
    assert engine is not None
    return {
        "cs_db_configured": engine.cs_db is not None,
        "cs_db_credentials_configured": bool(engine.settings.cloudstack_db.password),
        "cs_db_error": engine.cs_db_last_error,
        "auto_reconcile": engine.settings.auto_reconcile,
    }


# --- Sync log ---

@app.get("/api/logs")
def get_logs(
    limit: int = Query(50, le=200),
    _: None = Depends(require_operator),
):
    session = get_session()
    try:
        logs = session.query(SyncLog).order_by(SyncLog.timestamp.desc()).limit(limit).all()
        return [
            {
                "id": l.id,
                "timestamp": l.timestamp.isoformat() if l.timestamp else None,
                "action": l.action,
                "details": l.details,
                "success": l.success,
            }
            for l in logs
        ]
    finally:
        session.close()


# --- Dashboard summary ---

@app.get("/api/dashboard")
def dashboard():
    session = get_session()
    try:
        current_px = session.query(ProxmoxVM).filter_by(current=True)
        total_px = current_px.count()
        matched_px = current_px.filter_by(matched=True).count()
        running_px = current_px.filter_by(status="running").count()
        stopped_px = current_px.filter_by(status="stopped").count()
        stale_px = session.query(ProxmoxVM).filter_by(current=False).count()

        current_cs = session.query(CloudStackVM).filter_by(current=True)
        total_cs = current_cs.count()
        matched_cs = current_cs.filter_by(matched=True).count()
        stale_cs = session.query(CloudStackVM).filter_by(current=False).count()

        drift = engine.detect_drift()
        nic_drift = engine.detect_nic_drift()

        return {
            "proxmox": {
                "total": total_px,
                "matched": matched_px,
                "unmatched": total_px - matched_px,
                "running": running_px,
                "stopped": stopped_px,
                "stale": stale_px,
            },
            "cloudstack": {
                "total": total_cs,
                "matched": matched_cs,
                "unmatched": total_cs - matched_cs,
                "stale": stale_cs,
            },
            "drift_count": len(drift),
            "nic_drift_count": len(nic_drift),
            "last_sync": last_sync_result,
        }
    finally:
        session.close()


def _json_list(value: str | None) -> list:
    try:
        parsed = json.loads(value or "[]")
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


def _px_to_dict(v: ProxmoxVM) -> dict:
    return {
        "id": v.id,
        "cluster": v.cluster,
        "node": v.node,
        "vmid": v.vmid,
        "name": v.name,
        "status": v.status,
        "vm_type": v.vm_type,
        "template": v.template,
        "current": v.current,
        "config_current": bool(
            getattr(engine, "_nic_collection_ready", False)
            and v.config_current
        ),
        "cpus": v.cpus,
        "memory_mb": v.memory_mb,
        "disk_gb": v.disk_gb,
        "tags": v.tags,
        "networks": _json_list(v.networks),
        "storage": _json_list(v.storage),
        "cloudstack_uuid": v.cloudstack_uuid,
        "matched": v.matched,
        "match_source": v.match_source,
        "last_seen": v.last_seen.isoformat() if v.last_seen else None,
        "first_seen": v.first_seen.isoformat() if v.first_seen else None,
    }


def _cs_to_dict(v: CloudStackVM) -> dict:
    return {
        "uuid": v.uuid,
        "name": v.name,
        "display_name": v.display_name,
        "instance_name": v.instance_name,
        "state": v.state,
        "host_name": v.host_name,
        "host_id": v.host_id,
        "cluster_name": v.cluster_name,
        "zone_name": v.zone_name,
        "cpus": v.cpus,
        "memory_mb": v.memory_mb,
        "hypervisor": v.hypervisor,
        "proxmox_vmid": v.proxmox_vmid,
        "current": v.current,
        "proxmox_id": v.proxmox_id,
        "matched": v.matched,
        "match_source": v.match_source,
        "nics_current": bool(
            getattr(engine, "_nic_collection_ready", False)
            and v.nics_current
        ),
        "last_seen": v.last_seen.isoformat() if v.last_seen else None,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8088)
