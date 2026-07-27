from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime, timezone

Base = declarative_base()


class ProxmoxVM(Base):
    __tablename__ = "proxmox_vms"

    id = Column(String, primary_key=True)  # cluster:vmid
    cluster = Column(String, nullable=False, index=True)
    node = Column(String, nullable=False)
    vmid = Column(Integer, nullable=False)
    name = Column(String, nullable=False)
    status = Column(String, nullable=False)  # running, stopped, paused
    vm_type = Column(String, nullable=False)  # qemu, lxc
    template = Column(Boolean, nullable=False, default=False)
    current = Column(Boolean, nullable=False, default=False, index=True)
    config_current = Column(Boolean, nullable=False, default=False)
    cpus = Column(Integer, default=0)
    memory_mb = Column(Integer, default=0)
    disk_gb = Column(Float, default=0.0)
    networks = Column(Text, default="")
    storage = Column(Text, default="")
    tags = Column(String, default="")

    cloudstack_uuid = Column(String, nullable=True, index=True)
    matched = Column(Boolean, default=False)
    match_source = Column(String, default="")
    last_seen = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    first_seen = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class CloudStackVM(Base):
    __tablename__ = "cloudstack_vms"

    uuid = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    display_name = Column(String, default="")
    instance_name = Column(String, default="")
    state = Column(String, nullable=False)
    host_name = Column(String, default="")
    host_id = Column(String, default="")
    cluster_name = Column(String, default="")
    zone_name = Column(String, default="")
    cpus = Column(Integer, default=0)
    memory_mb = Column(Integer, default=0)
    hypervisor = Column(String, default="")
    proxmox_vmid = Column(Integer, nullable=True, index=True)
    current = Column(Boolean, nullable=False, default=False, index=True)

    proxmox_id = Column(String, nullable=True, index=True)
    matched = Column(Boolean, default=False)
    match_source = Column(String, default="")
    nics = Column(Text, default="")  # JSON snapshot of CloudStack nics for this VM
    nics_current = Column(Boolean, nullable=False, default=False)
    last_seen = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class HostMapping(Base):
    __tablename__ = "host_mappings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    proxmox_cluster = Column(String, nullable=False, index=True)
    proxmox_node = Column(String, nullable=False)
    cloudstack_host_id = Column(String, nullable=False)
    cloudstack_host_name = Column(String, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class NetworkMapping(Base):
    __tablename__ = "network_mappings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    proxmox_cluster = Column(String, nullable=False, index=True)
    proxmox_bridge = Column(String, nullable=False)  # e.g. vmbr0
    proxmox_vlan = Column(Integer, nullable=True)  # VLAN tag, NULL = untagged
    cloudstack_network_id = Column(String, nullable=False)  # CS networks.id or uuid
    cloudstack_network_name = Column(String, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class SyncLog(Base):
    __tablename__ = "sync_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    action = Column(String, nullable=False)
    details = Column(Text, default="")
    success = Column(Boolean, default=True)


class AdoptionClaim(Base):
    """Authoritative source-free claim on an existing Proxmox QEMU VM.

    A durable unique row prevents two CloudStack deployment transactions from
    adopting the same cluster-local VMID.  The one-time nonce is stored only as
    a SHA-256 digest; it is never returned after reservation creation.
    """

    __tablename__ = "adoption_claims"
    __table_args__ = (
        UniqueConstraint(
            "proxmox_cluster",
            "proxmox_vmid",
            name="uq_adoption_claim_cluster_vmid",
        ),
        UniqueConstraint(
            "cloudstack_vm_ref",
            name="uq_adoption_claim_cloudstack_vm_ref",
        ),
    )

    id = Column(String(36), primary_key=True)
    proxmox_cluster = Column(String, nullable=False, index=True)
    proxmox_node = Column(String, nullable=False)
    proxmox_vmid = Column(Integer, nullable=False)
    manifest_sha256 = Column(String(64), nullable=False)
    manifest_json = Column(Text, nullable=False)
    nonce_sha256 = Column(String(64), nullable=False)
    generation = Column(Integer, nullable=False, default=1)
    state = Column(String(16), nullable=False, default="reserved", index=True)
    cloudstack_vm_ref = Column(String, nullable=True)
    cloudstack_instance_name = Column(String, nullable=True, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


_engine = None
_SessionLocal = None


def init_db(database_url: str):
    global _engine, _SessionLocal
    _engine = create_engine(database_url, echo=False)
    Base.metadata.create_all(_engine)
    _run_lightweight_migrations(_engine)
    _SessionLocal = sessionmaker(bind=_engine)


def _run_lightweight_migrations(engine):
    """Add columns introduced after a DB was first created.

    create_all() only creates missing tables, never alters existing ones, so
    new columns on pre-existing tables (e.g. cloudstack_vms.nics) must be added
    explicitly. Idempotent: only adds a column when it's absent.
    """
    inspector = inspect(engine)
    additions = {
        "cloudstack_vms": [
            ("nics", "TEXT DEFAULT ''"),
            ("proxmox_vmid", "INTEGER"),
            ("current", "BOOLEAN NOT NULL DEFAULT 0"),
            ("match_source", "VARCHAR DEFAULT ''"),
            ("nics_current", "BOOLEAN NOT NULL DEFAULT 0"),
        ],
        "proxmox_vms": [
            ("networks", "TEXT DEFAULT ''"),
            ("storage", "TEXT DEFAULT ''"),
            ("template", "BOOLEAN NOT NULL DEFAULT 0"),
            ("current", "BOOLEAN NOT NULL DEFAULT 0"),
            ("config_current", "BOOLEAN NOT NULL DEFAULT 0"),
            ("match_source", "VARCHAR DEFAULT ''"),
        ],
    }
    with engine.begin() as conn:
        for table, columns in additions.items():
            if not inspector.has_table(table):
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            for name, ddl in columns:
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def get_session():
    return _SessionLocal()
