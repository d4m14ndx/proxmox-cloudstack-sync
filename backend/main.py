import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import load_settings
from database import init_db, get_session, ProxmoxVM, CloudStackVM, HostMapping, SyncLog
from sync_engine import SyncEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

settings = load_settings()
engine: SyncEngine | None = None
scheduler = BackgroundScheduler()
last_sync_result: dict = {}


def run_sync():
    global last_sync_result
    try:
        last_sync_result = engine.full_sync()
        last_sync_result["timestamp"] = datetime.now(timezone.utc).isoformat()
    except Exception as e:
        log.error(f"Sync failed: {e}")
        last_sync_result = {"error": str(e), "timestamp": datetime.now(timezone.utc).isoformat()}


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
async def index():
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
        "auto_reconcile": settings.auto_reconcile,
    }


@app.post("/api/sync")
async def trigger_sync():
    run_sync()
    return last_sync_result


# --- Proxmox VM endpoints ---

@app.get("/api/proxmox/vms")
async def list_proxmox_vms(
    cluster: str | None = None,
    matched: bool | None = None,
    status: str | None = None,
):
    session = get_session()
    try:
        q = session.query(ProxmoxVM)
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
async def list_proxmox_clusters():
    session = get_session()
    try:
        rows = session.query(
            ProxmoxVM.cluster,
        ).distinct().all()
        clusters = []
        for (cluster_name,) in rows:
            count = session.query(ProxmoxVM).filter_by(cluster=cluster_name).count()
            matched_count = session.query(ProxmoxVM).filter_by(cluster=cluster_name, matched=True).count()
            clusters.append({
                "name": cluster_name,
                "total_vms": count,
                "matched_vms": matched_count,
                "unmatched_vms": count - matched_count,
            })
        return clusters
    finally:
        session.close()


# --- CloudStack VM endpoints ---

@app.get("/api/cloudstack/vms")
async def list_cloudstack_vms(matched: bool | None = None):
    session = get_session()
    try:
        q = session.query(CloudStackVM)
        if matched is not None:
            q = q.filter(CloudStackVM.matched == matched)
        vms = q.order_by(CloudStackVM.name).all()
        return [_cs_to_dict(v) for v in vms]
    finally:
        session.close()


@app.get("/api/cloudstack/hosts")
async def list_cloudstack_hosts():
    if not engine.cs_client:
        raise HTTPException(400, "CloudStack not configured")
    return engine.cs_client.list_hosts()


@app.get("/api/cloudstack/clusters")
async def list_cs_clusters():
    if not engine.cs_client:
        raise HTTPException(400, "CloudStack not configured")
    return engine.cs_client.list_clusters()


@app.get("/api/cloudstack/zones")
async def list_cs_zones():
    if not engine.cs_client:
        raise HTTPException(400, "CloudStack not configured")
    return engine.cs_client.list_zones()


@app.get("/api/cloudstack/service-offerings")
async def list_service_offerings():
    if not engine.cs_client:
        raise HTTPException(400, "CloudStack not configured")
    return engine.cs_client.list_service_offerings()


@app.get("/api/cloudstack/networks")
async def list_cs_networks():
    if not engine.cs_client:
        raise HTTPException(400, "CloudStack not configured")
    return engine.cs_client.list_networks()


@app.get("/api/cloudstack/disk-offerings")
async def list_cs_disk_offerings():
    if not engine.cs_client:
        raise HTTPException(400, "CloudStack not configured")
    return engine.cs_client.list_disk_offerings()


# --- Drift detection ---

@app.get("/api/drift")
async def get_drift():
    return engine.detect_drift()


# --- Matching ---

class MatchRequest(BaseModel):
    proxmox_id: str
    cloudstack_uuid: str


@app.post("/api/match")
async def manual_match(req: MatchRequest):
    session = get_session()
    try:
        px = session.query(ProxmoxVM).filter_by(id=req.proxmox_id).first()
        cs = session.query(CloudStackVM).filter_by(uuid=req.cloudstack_uuid).first()
        if not px:
            raise HTTPException(404, f"Proxmox VM {req.proxmox_id} not found")
        if not cs:
            raise HTTPException(404, f"CloudStack VM {req.cloudstack_uuid} not found")

        px.matched = True
        px.cloudstack_uuid = cs.uuid
        cs.matched = True
        cs.proxmox_id = px.id
        session.commit()

        engine._log(session, "manual_match",
                    f"Matched {px.name} ({px.id}) <-> {cs.name} ({cs.uuid})")
        session.commit()
        return {"status": "matched", "proxmox": _px_to_dict(px), "cloudstack": _cs_to_dict(cs)}
    finally:
        session.close()


@app.post("/api/unmatch/{proxmox_id}")
async def unmatch_vm(proxmox_id: str):
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
        px.matched = False
        px.cloudstack_uuid = None
        session.commit()
        return {"status": "unmatched"}
    finally:
        session.close()


# --- Import / Register ---

class RegisterRequest(BaseModel):
    proxmox_id: str
    cs_host_id: int
    service_offering_id: int
    account_id: int
    domain_id: int
    zone_id: int
    pod_id: int | None = None
    guest_os_id: int = 1


@app.post("/api/register")
async def register_vm(req: RegisterRequest):
    """Register an existing Proxmox VM into CloudStack by writing DB records."""
    if not engine.cs_db:
        raise HTTPException(400, "CloudStack DB not configured")

    session = get_session()
    try:
        px = session.query(ProxmoxVM).filter_by(id=req.proxmox_id).first()
        if not px:
            raise HTTPException(404, "Proxmox VM not found")

        host = engine.cs_db.get_host_by_id(req.cs_host_id)
        if not host:
            raise HTTPException(404, f"CloudStack host {req.cs_host_id} not found")

        params = {
            "name": px.name,
            "instance_name": px.name,
            "host_id": req.cs_host_id,
            "zone_id": req.zone_id,
            "pod_id": req.pod_id or host.get("pod_id"),
            "cluster_id": host.get("cluster_id"),
            "service_offering_id": req.service_offering_id,
            "account_id": req.account_id,
            "domain_id": req.domain_id,
            "guest_os_id": req.guest_os_id,
            "hypervisor_type": "External",
            "proxmox_vmid": px.vmid,
            "state": "Running" if px.status == "running" else "Stopped",
        }

        result = engine.cs_db.register_existing_vm(params)
        if not result:
            raise HTTPException(500, "Failed to register VM")

        engine._log(session, "register",
                    f"Registered {px.name} (VMID {px.vmid}) into CloudStack "
                    f"as {result['uuid']}")
        session.commit()

        return {"status": "registered", "result": result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Registration failed: {e}")
    finally:
        session.close()


@app.get("/api/cloudstack/db-hosts")
async def list_db_hosts():
    """List hosts from CloudStack DB (includes zone/cluster context for registration)."""
    if not engine.cs_db:
        raise HTTPException(400, "CloudStack DB not configured")
    return engine.cs_db.list_hosts()


@app.get("/api/cloudstack/db-accounts")
async def list_db_accounts():
    """List accounts from CloudStack DB for registration."""
    if not engine.cs_db:
        raise HTTPException(400, "CloudStack DB not configured")
    try:
        import pymysql
        conn = pymysql.connect(
            host=engine.settings.cloudstack_db.host,
            port=engine.settings.cloudstack_db.port,
            user=engine.settings.cloudstack_db.user,
            password=engine.settings.cloudstack_db.password,
            database=engine.settings.cloudstack_db.database,
            cursorclass=pymysql.cursors.DictCursor,
        )
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
        raise HTTPException(500, str(e))


@app.get("/api/cloudstack/db-service-offerings")
async def list_db_service_offerings():
    """List service offerings from CloudStack DB for registration."""
    if not engine.cs_db:
        raise HTTPException(400, "CloudStack DB not configured")
    try:
        import pymysql
        conn = pymysql.connect(
            host=engine.settings.cloudstack_db.host,
            port=engine.settings.cloudstack_db.port,
            user=engine.settings.cloudstack_db.user,
            password=engine.settings.cloudstack_db.password,
            database=engine.settings.cloudstack_db.database,
            cursorclass=pymysql.cursors.DictCursor,
        )
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
        raise HTTPException(500, str(e))


@app.get("/api/cloudstack/db-guest-os")
async def list_db_guest_os():
    """List guest OS types from CloudStack DB."""
    if not engine.cs_db:
        raise HTTPException(400, "CloudStack DB not configured")
    try:
        import pymysql
        conn = pymysql.connect(
            host=engine.settings.cloudstack_db.host,
            port=engine.settings.cloudstack_db.port,
            user=engine.settings.cloudstack_db.user,
            password=engine.settings.cloudstack_db.password,
            database=engine.settings.cloudstack_db.database,
            cursorclass=pymysql.cursors.DictCursor,
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, uuid, display_name FROM guest_os "
                "WHERE removed IS NULL ORDER BY display_name LIMIT 200"
            )
            return cur.fetchall()
    except Exception as e:
        raise HTTPException(500, str(e))


# --- Host mappings ---

class HostMappingRequest(BaseModel):
    proxmox_cluster: str
    proxmox_node: str
    cloudstack_host_id: str
    cloudstack_host_name: str


@app.get("/api/host-mappings")
async def list_host_mappings():
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
async def create_host_mapping(req: HostMappingRequest):
    session = get_session()
    try:
        existing = session.query(HostMapping).filter_by(
            proxmox_cluster=req.proxmox_cluster,
            proxmox_node=req.proxmox_node,
        ).first()
        if existing:
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
async def delete_host_mapping(mapping_id: int):
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
async def list_proxmox_nodes():
    """List unique proxmox cluster/node pairs from discovered VMs."""
    session = get_session()
    try:
        rows = session.query(
            ProxmoxVM.cluster, ProxmoxVM.node
        ).distinct().order_by(ProxmoxVM.cluster, ProxmoxVM.node).all()
        return [{"cluster": r[0], "node": r[1]} for r in rows]
    finally:
        session.close()


# --- Reconciliation ---

class ReconcileVmRequest(BaseModel):
    drift_item: dict


@app.post("/api/reconcile/vm")
async def reconcile_vm(req: ReconcileVmRequest):
    if not engine.cs_db:
        raise HTTPException(400, "CloudStack DB not configured")
    return engine.reconcile_vm(req.drift_item)


@app.post("/api/reconcile/all")
async def reconcile_all():
    return engine.reconcile_all()


@app.get("/api/reconcile/status")
async def reconcile_status():
    return {
        "cs_db_configured": engine.cs_db is not None,
        "auto_reconcile": engine.settings.auto_reconcile,
    }


# --- Sync log ---

@app.get("/api/logs")
async def get_logs(limit: int = Query(50, le=200)):
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
async def dashboard():
    session = get_session()
    try:
        total_px = session.query(ProxmoxVM).count()
        matched_px = session.query(ProxmoxVM).filter_by(matched=True).count()
        running_px = session.query(ProxmoxVM).filter_by(status="running").count()
        stopped_px = session.query(ProxmoxVM).filter_by(status="stopped").count()

        total_cs = session.query(CloudStackVM).count()
        matched_cs = session.query(CloudStackVM).filter_by(matched=True).count()

        drift = engine.detect_drift()

        return {
            "proxmox": {
                "total": total_px,
                "matched": matched_px,
                "unmatched": total_px - matched_px,
                "running": running_px,
                "stopped": stopped_px,
            },
            "cloudstack": {
                "total": total_cs,
                "matched": matched_cs,
                "unmatched": total_cs - matched_cs,
            },
            "drift_count": len(drift),
            "last_sync": last_sync_result,
        }
    finally:
        session.close()


def _px_to_dict(v: ProxmoxVM) -> dict:
    return {
        "id": v.id,
        "cluster": v.cluster,
        "node": v.node,
        "vmid": v.vmid,
        "name": v.name,
        "status": v.status,
        "vm_type": v.vm_type,
        "cpus": v.cpus,
        "memory_mb": v.memory_mb,
        "disk_gb": v.disk_gb,
        "tags": v.tags,
        "cloudstack_uuid": v.cloudstack_uuid,
        "matched": v.matched,
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
        "proxmox_id": v.proxmox_id,
        "matched": v.matched,
        "last_seen": v.last_seen.isoformat() if v.last_seen else None,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8088)
