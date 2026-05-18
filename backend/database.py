from sqlalchemy import create_engine, Column, String, Integer, Boolean, DateTime, Float, Text
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
    cpus = Column(Integer, default=0)
    memory_mb = Column(Integer, default=0)
    disk_gb = Column(Float, default=0.0)
    networks = Column(Text, default="")
    tags = Column(String, default="")

    cloudstack_uuid = Column(String, nullable=True, index=True)
    matched = Column(Boolean, default=False)
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

    proxmox_id = Column(String, nullable=True, index=True)
    matched = Column(Boolean, default=False)
    last_seen = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class SyncLog(Base):
    __tablename__ = "sync_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    action = Column(String, nullable=False)
    details = Column(Text, default="")
    success = Column(Boolean, default=True)


_engine = None
_SessionLocal = None


def init_db(database_url: str):
    global _engine, _SessionLocal
    _engine = create_engine(database_url, echo=False)
    Base.metadata.create_all(_engine)
    _SessionLocal = sessionmaker(bind=_engine)


def get_session():
    return _SessionLocal()
