import ipaddress
import logging
import secrets
import string
import uuid as uuid_mod
from datetime import datetime
import pymysql
from config import CloudStackDBConfig

log = logging.getLogger(__name__)


def _netmask_from_cidr(cidr: str) -> str | None:
    """Derive a dotted netmask from a CIDR like '10.0.0.0/24'."""
    if not cidr or "/" not in cidr:
        return None
    try:
        return str(ipaddress.IPv4Network(cidr, strict=False).netmask)
    except Exception:
        return None


class CloudStackDB:
    def __init__(self, config: CloudStackDBConfig):
        self._config = config

    def _connect(self):
        return pymysql.connect(
            host=self._config.host,
            port=self._config.port,
            user=self._config.user,
            password=self._config.password,
            database=self._config.database,
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
        )

    def get_vm_by_uuid(self, uuid: str) -> dict | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, uuid, instance_name, name, state, host_id, "
                    "last_host_id, power_state, power_host, hypervisor_type, "
                    "data_center_id, account_id, domain_id, service_offering_id "
                    "FROM vm_instance WHERE uuid = %s AND removed IS NULL",
                    (uuid,),
                )
                return cur.fetchone()

    def get_host_by_id(self, host_id: int) -> dict | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, uuid, name, cluster_id, pod_id, data_center_id, status "
                    "FROM host WHERE id = %s AND removed IS NULL",
                    (host_id,),
                )
                return cur.fetchone()

    def get_host_by_name(self, name: str) -> dict | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, uuid, name, cluster_id, pod_id, data_center_id, status "
                    "FROM host WHERE name = %s AND removed IS NULL",
                    (name,),
                )
                return cur.fetchone()

    def get_host_by_uuid(self, uuid: str) -> dict | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, uuid, name, cluster_id, pod_id, data_center_id, status "
                    "FROM host WHERE uuid = %s AND removed IS NULL",
                    (uuid,),
                )
                return cur.fetchone()

    def update_vm_host(self, vm_uuid: str, new_host_id: int, old_host_id: int | None = None) -> bool:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE vm_instance SET "
                    "  host_id = %s, "
                    "  last_host_id = %s, "
                    "  update_time = NOW(), "
                    "  update_count = update_count + 1 "
                    "WHERE uuid = %s AND removed IS NULL",
                    (new_host_id, old_host_id, vm_uuid),
                )
                conn.commit()
                updated = cur.rowcount > 0
                if updated:
                    log.info(f"Updated host for VM {vm_uuid}: {old_host_id} -> {new_host_id}")
                return updated

    def update_vm_placement_and_state(self, vm_uuid: str, new_host_id: int | None,
                                       power_state: str, vm_state: str,
                                       old_host_id: int | None = None) -> bool:
        """Atomic update of host placement, power state, and lifecycle state.

        For Running VMs: host_id = new_host_id, power_host = new_host_id
        For Stopped VMs: host_id = NULL, last_host_id = old_host_id, power_host = NULL
        """
        power_host = new_host_id if vm_state == "Running" else None
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE vm_instance SET "
                    "  host_id = %s, "
                    "  last_host_id = %s, "
                    "  state = %s, "
                    "  power_state = %s, "
                    "  power_host = %s, "
                    "  power_state_update_time = NOW(), "
                    "  power_state_update_count = 0, "
                    "  update_time = NOW(), "
                    "  update_count = update_count + 1 "
                    "WHERE uuid = %s AND removed IS NULL",
                    (new_host_id, old_host_id, vm_state, power_state,
                     power_host, vm_uuid),
                )
                conn.commit()
                updated = cur.rowcount > 0
                if updated:
                    log.info(f"Updated VM {vm_uuid}: host={new_host_id}, "
                             f"last_host={old_host_id}, state={vm_state}, "
                             f"power={power_state}, power_host={power_host}")
                return updated

    def get_import_template_id(self) -> int | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM vm_template "
                    "WHERE name IN (%s, %s) AND removed IS NULL LIMIT 1",
                    ("kvm-default-vm-import-dummy-template",
                     "system-default-vm-import-dummy-template.iso"),
                )
                row = cur.fetchone()
                if row:
                    return row["id"]
                cur.execute(
                    "SELECT id FROM vm_template "
                    "WHERE removed IS NULL AND type != 'SYSTEM' "
                    "ORDER BY id LIMIT 1",
                )
                row = cur.fetchone()
                return row["id"] if row else None

    @staticmethod
    def _generate_vnc_password(length: int = 8) -> str:
        alphabet = string.ascii_letters + string.digits
        return ''.join(secrets.choice(alphabet) for _ in range(length))

    def register_existing_vm(self, params: dict) -> dict | None:
        """Register an existing Proxmox VM into CloudStack by creating DB records.

        params: name, instance_name, host_id, zone_id, pod_id,
                service_offering_id, account_id, domain_id, guest_os_id,
                hypervisor_type, proxmox_vmid, state,
                vm_template_id, private_mac_address
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM sequence WHERE name = 'vm_instance_seq' FOR UPDATE")
                row = cur.fetchone()
                vm_id = row["value"]
                cur.execute("UPDATE sequence SET value = value + 1 WHERE name = 'vm_instance_seq'")

                import uuid as uuid_mod
                vm_uuid = str(uuid_mod.uuid4())
                instance_name = params.get("instance_name", f"i-{vm_id}-VM")
                state = params.get("state", "Running")
                power_state = "PowerOn" if state == "Running" else "PowerOff"
                host_id = params["host_id"] if state == "Running" else None
                template_id = params.get("vm_template_id")
                mac_address = params.get("private_mac_address", "00:00:00:00:00:00")
                vnc_password = self._generate_vnc_password()

                cur.execute(
                    "INSERT INTO vm_instance ("
                    "  id, name, uuid, instance_name, state, vm_template_id, "
                    "  guest_os_id, private_mac_address, pod_id, data_center_id, "
                    "  host_id, last_host_id, "
                    "  vnc_password, ha_enabled, display_vm, `type`, vm_type, "
                    "  account_id, user_id, "
                    "  domain_id, service_offering_id, hypervisor_type, "
                    "  power_state, power_host, power_state_update_time, "
                    "  power_state_update_count, created, update_time, update_count"
                    ") VALUES ("
                    "  %s, %s, %s, %s, %s, %s, "
                    "  %s, %s, %s, %s, "
                    "  %s, %s, "
                    "  %s, 0, 1, 'User', 'User', "
                    "  %s, 1, "
                    "  %s, %s, %s, "
                    "  %s, %s, NOW(), "
                    "  0, NOW(), NOW(), 0"
                    ")",
                    (vm_id, params["name"], vm_uuid, instance_name, state, template_id,
                     params.get("guest_os_id", 1), mac_address, params.get("pod_id"),
                     params["zone_id"],
                     host_id, host_id,
                     vnc_password,
                     params["account_id"],
                     params["domain_id"], params["service_offering_id"],
                     params.get("hypervisor_type", "External"),
                     power_state, host_id),
                )

                cur.execute(
                    "INSERT INTO user_vm (id, display_name, update_parameters) "
                    "VALUES (%s, %s, 1)",
                    (vm_id, params["name"]),
                )

                cur.execute(
                    "INSERT INTO vm_instance_details (vm_id, name, value) "
                    "VALUES (%s, 'proxmox_vmid', %s)",
                    (vm_id, str(params["proxmox_vmid"])),
                )

                conn.commit()
                log.info(f"Registered VM {params['name']} as {vm_uuid} (id={vm_id})")
                return {
                    "id": vm_id,
                    "uuid": vm_uuid,
                    "instance_name": instance_name,
                    "name": params["name"],
                    "state": state,
                }

    def repair_registered_vm(self, vm_uuid: str, template_id: int | None,
                              mac_address: str | None, vnc_password: str | None) -> bool:
        with self._connect() as conn:
            with conn.cursor() as cur:
                sets = ["update_time = NOW()"]
                vals = []
                if template_id is not None:
                    sets.append("vm_template_id = %s")
                    vals.append(template_id)
                if mac_address:
                    sets.append("private_mac_address = %s")
                    vals.append(mac_address)
                if vnc_password:
                    sets.append("vnc_password = %s")
                    vals.append(vnc_password)
                vals.append(vm_uuid)
                cur.execute(
                    f"UPDATE vm_instance SET {', '.join(sets)} "
                    "WHERE uuid = %s AND removed IS NULL",
                    tuple(vals),
                )
                conn.commit()
                updated = cur.rowcount > 0
                if updated:
                    log.info(f"Repaired VM {vm_uuid}: template={template_id}, mac={mac_address}")
                return updated

    def list_hosts(self) -> list[dict]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT h.id, h.uuid, h.name, h.status, h.cluster_id, "
                    "h.pod_id, h.data_center_id, h.hypervisor_type, "
                    "c.name as cluster_name, dc.name as zone_name "
                    "FROM host h "
                    "LEFT JOIN cluster c ON h.cluster_id = c.id "
                    "LEFT JOIN data_center dc ON h.data_center_id = dc.id "
                    "WHERE h.removed IS NULL AND h.type = 'Routing' "
                    "ORDER BY h.name",
                )
                return cur.fetchall()

    def get_vm_details(self, vm_uuid: str) -> list[dict]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT uvd.name, uvd.value FROM vm_instance_details uvd "
                    "JOIN vm_instance vi ON uvd.vm_id = vi.id "
                    "WHERE vi.uuid = %s",
                    (vm_uuid,),
                )
                return cur.fetchall()

    # --- NIC management ---

    def get_nics_columns(self) -> set:
        """Introspect the nics table column set (preflight for robust inserts)."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = 'nics'",
                    (self._config.database,),
                )
                return {r["column_name"] for r in cur.fetchall()}

    def list_networks(self) -> list[dict]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT n.id, n.uuid, n.name, n.broadcast_uri, n.gateway, "
                    "n.cidr, n.mode, n.traffic_type, n.broadcast_domain_type, "
                    "n.physical_network_id, n.data_center_id, n.removed, "
                    "dc.name as zone_name "
                    "FROM networks n "
                    "LEFT JOIN data_center dc ON n.data_center_id = dc.id "
                    "WHERE n.removed IS NULL "
                    "ORDER BY n.name"
                )
                rows = cur.fetchall()
                for r in rows:
                    r["netmask"] = _netmask_from_cidr(r.get("cidr"))
                return rows

    def get_network(self, network_ref: str) -> dict | None:
        """Resolve a network by integer id or uuid."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        "SELECT id, uuid, name, broadcast_uri, gateway, cidr, mode, "
                        "traffic_type, broadcast_domain_type "
                        "FROM networks WHERE id = %s AND removed IS NULL",
                        (int(network_ref),),
                    )
                except (ValueError, TypeError):
                    cur.execute(
                        "SELECT id, uuid, name, broadcast_uri, gateway, cidr, mode, "
                        "traffic_type, broadcast_domain_type "
                        "FROM networks WHERE uuid = %s AND removed IS NULL",
                        (network_ref,),
                    )
                row = cur.fetchone()
                if row:
                    row["netmask"] = _netmask_from_cidr(row.get("cidr"))
                return row

    def get_vm_nics(self, instance_id: int) -> list[dict]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ni.id, ni.uuid, ni.mac_address, ni.ip4_address, "
                    "ni.netmask, ni.gateway, ni.network_id, ni.device_id, "
                    "ni.default_nic, ni.state, ni.strategy, ni.broadcast_uri, "
                    "n.name as network_name, n.uuid as network_uuid "
                    "FROM nics ni "
                    "LEFT JOIN networks n ON ni.network_id = n.id "
                    "WHERE ni.instance_id = %s AND ni.removed IS NULL "
                    "ORDER BY ni.device_id",
                    (instance_id,),
                )
                return cur.fetchall()

    def sample_nic_on_network(self, network_id: int) -> dict | None:
        """Fetch one existing live NIC on a network to copy column conventions
        (state/strategy/reserver_name/reservation_id/uri/mode/vm_type)."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM nics WHERE network_id = %s AND removed IS NULL "
                    "ORDER BY id DESC LIMIT 1",
                    (network_id,),
                )
                return cur.fetchone()

    def _build_nic_row(self, params: dict, columns: set, sample: dict | None) -> dict:
        """Assemble a column->value map for an INSERT, filtered to real columns.

        params: instance_id, network_id, mac, device_id, default_nic, running,
                ip, netmask, gateway, vm_type
        sample: an existing NIC row on the same network (conventions), or None.
        """
        sample = sample or {}
        running = params.get("running", True)
        candidate = {
            "uuid": str(uuid_mod.uuid4()),
            "instance_id": params["instance_id"],
            "mac_address": params.get("mac") or None,
            "network_id": params["network_id"],
            "ip4_address": params.get("ip"),
            "netmask": params.get("netmask"),
            "gateway": params.get("gateway"),
            "ip_type": sample.get("ip_type") or "Ip4",
            "broadcast_uri": sample.get("broadcast_uri"),
            "isolation_uri": sample.get("isolation_uri"),
            "mode": sample.get("mode") or "Dhcp",
            "state": sample.get("state") or ("Reserved" if running else "Allocated"),
            "strategy": sample.get("strategy") or "Start",
            "reserver_name": sample.get("reserver_name"),
            "reservation_id": sample.get("reservation_id"),
            "device_id": params.get("device_id", 0),
            "default_nic": 1 if params.get("default_nic") else 0,
            "vm_type": sample.get("vm_type") or params.get("vm_type") or "User",
            "secondary_ip": 0,
            "display_nic": 1,
        }
        # Only keep columns that actually exist in this CloudStack version
        return {k: v for k, v in candidate.items() if k in columns}

    def insert_nic(self, params: dict, dry_run: bool = False) -> dict:
        """Insert a NIC row for a VM. If params['default_nic'], clears any
        existing default_nic on the VM first (CloudStack expects exactly one)."""
        columns = self.get_nics_columns()
        sample = self.sample_nic_on_network(params["network_id"])
        row = self._build_nic_row(params, columns, sample)

        col_names = list(row.keys())
        placeholders = ", ".join(["%s"] * len(col_names))
        extra_cols, extra_vals = [], []
        if "created" in columns:
            extra_cols.append("created")
            extra_vals.append("NOW()")
        if "update_time" in columns:
            extra_cols.append("update_time")
            extra_vals.append("NOW()")
        all_cols = col_names + extra_cols
        sql = (
            f"INSERT INTO nics ({', '.join(all_cols)}) "
            f"VALUES ({', '.join([placeholders] + extra_vals)})"
        )
        values = [row[c] for c in col_names]

        if dry_run:
            return {"dry_run": True, "sql": sql, "values": values, "uuid": row.get("uuid")}

        with self._connect() as conn:
            with conn.cursor() as cur:
                if params.get("default_nic"):
                    cur.execute(
                        "UPDATE nics SET default_nic = 0 "
                        "WHERE instance_id = %s AND removed IS NULL",
                        (params["instance_id"],),
                    )
                cur.execute(sql, tuple(values))
                conn.commit()
                log.info(f"Inserted NIC {row.get('uuid')} for instance "
                         f"{params['instance_id']} on network {params['network_id']} "
                         f"(mac={row.get('mac_address')}, ip={row.get('ip4_address')})")
                return {"status": "inserted", "uuid": row.get("uuid"),
                        "mac": row.get("mac_address"), "ip": row.get("ip4_address")}

    def update_nic(self, nic_id: int, fields: dict, dry_run: bool = False) -> dict:
        """Update selected fields on an existing NIC row.

        fields may include: mac_address, network_id, ip4_address, netmask, gateway.
        """
        allowed = {"mac_address", "network_id", "ip4_address", "netmask",
                   "gateway", "broadcast_uri", "isolation_uri"}
        sets, vals = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k} = %s")
                vals.append(v)
        if not sets:
            return {"error": "no updatable fields supplied"}
        sql = (f"UPDATE nics SET {', '.join(sets)}, update_time = NOW() "
               "WHERE id = %s AND removed IS NULL")
        vals.append(nic_id)

        if dry_run:
            return {"dry_run": True, "sql": sql, "values": vals}

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(vals))
                conn.commit()
                ok = cur.rowcount > 0
                if ok:
                    log.info(f"Updated NIC {nic_id}: {fields}")
                return {"status": "updated" if ok else "no_change", "nic_id": nic_id}

    def remove_nic(self, nic_id: int, dry_run: bool = False) -> dict:
        """Soft-remove a NIC (CloudStack convention: set removed = NOW())."""
        sql = "UPDATE nics SET removed = NOW(), state = 'Deallocating' WHERE id = %s AND removed IS NULL"
        if dry_run:
            return {"dry_run": True, "sql": sql, "values": [nic_id]}
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (nic_id,))
                conn.commit()
                ok = cur.rowcount > 0
                if ok:
                    log.info(f"Removed NIC {nic_id}")
                return {"status": "removed" if ok else "no_change", "nic_id": nic_id}

    def test_connection(self) -> bool:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
            return True
        except Exception as e:
            log.error(f"CloudStack DB connection failed: {e}")
            return False
