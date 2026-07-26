import logging
import json
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import load_settings
from database import init_db, get_session, ProxmoxVM, CloudStackVM, HostMapping, NetworkMapping, SyncLog
from sync_engine import SyncEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

settings = load_settings()
engine: SyncEngine | None = None
scheduler = BackgroundScheduler()
last_sync_result: dict = {}
sync_lock = threading.Lock()


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine
    init_db(settings.database_url)
    engine = SyncEngine(settings)

    scheduler.add_job(run_sync, "interval", seconds=settings.sync_interval_seconds, id="sync_job")
    scheduler.start()
    run_sync()
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
        host_mappings = {
            (
                SyncEngine._canonical_mapping_value(m.proxmox_cluster),
                SyncEngine._canonical_mapping_value(m.proxmox_node),
            )
            for m in SyncEngine._globally_unique_host_mappings(session)
        }
        network_mappings = {
            (m.proxmox_cluster, m.proxmox_bridge.lower(), m.proxmox_vlan)
            for m in session.query(NetworkMapping).all()
        }
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
                if placement not in host_mappings:
                    blockers.append("host_mapping_missing")
                if not inventory_collection_current:
                    blockers.append("inventory_collection_not_current")
                if not config_current:
                    blockers.append("config_snapshot_not_current")
                else:
                    if not networks:
                        blockers.append("nic_inventory_missing")
                    for nic in networks:
                        if not nic.get("mac") or not nic.get("bridge"):
                            blockers.append(
                                f"nic{nic.get('device_id', '?')}_identity_incomplete"
                            )
                            continue
                        key = (
                            px.cluster,
                            nic["bridge"].lower(),
                            nic.get("vlan"),
                        )
                        if key not in network_mappings:
                            blockers.append(
                                f"network_mapping_missing:{nic['bridge']}:"
                                f"{nic.get('vlan') if nic.get('vlan') is not None else 'untagged'}"
                            )
                    data_disks = [
                        d for d in storage if d.get("media") != "cdrom"
                    ]
                    if not data_disks:
                        blockers.append("storage_inventory_missing")
                    elif any(
                        not d.get("volume") or not d.get("storage")
                        for d in data_disks
                    ):
                        blockers.append("storage_identity_incomplete")
                blockers.extend([
                    "cloudstack_account_domain_project_mapping_required",
                    "service_offering_mapping_required",
                    "adopt_existing_orchestrator_not_implemented",
                ])

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
            "ready": 0,
        }
        return {
            "summary": summary,
            "freshness": {
                "inventory_collection_current": inventory_collection_current,
                "nic_collection_current": nic_collection_current,
            },
            "candidates": rows,
        }
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
