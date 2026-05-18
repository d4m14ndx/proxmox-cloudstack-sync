import logging
from datetime import datetime
import pymysql
from config import CloudStackDBConfig

log = logging.getLogger(__name__)


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

    def register_existing_vm(self, params: dict) -> dict | None:
        """Register an existing Proxmox VM into CloudStack by creating DB records.

        params: name, instance_name, host_id, zone_id, pod_id, cluster_id,
                service_offering_id, account_id, domain_id, guest_os_id,
                hypervisor_type, proxmox_vmid, cpus, memory_mb, state
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                # Get next VM ID
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

                cur.execute(
                    "INSERT INTO vm_instance ("
                    "  id, name, uuid, instance_name, state, vm_template_id, "
                    "  guest_os_id, pod_id, data_center_id, host_id, last_host_id, "
                    "  vnc_password, ha_enabled, `type`, vm_type, account_id, "
                    "  domain_id, service_offering_id, hypervisor_type, "
                    "  power_state, power_host, power_state_update_time, "
                    "  power_state_update_count, created, update_count"
                    ") VALUES ("
                    "  %s, %s, %s, %s, %s, NULL, "
                    "  %s, %s, %s, %s, %s, "
                    "  '', 0, 'User', 'User', %s, "
                    "  %s, %s, %s, "
                    "  %s, %s, NOW(), "
                    "  0, NOW(), 0"
                    ")",
                    (vm_id, params["name"], vm_uuid, instance_name, state,
                     params.get("guest_os_id", 1),
                     params.get("pod_id"), params["zone_id"], host_id, host_id,
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
                    "INSERT INTO user_vm_details (vm_id, name, value) "
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

    def list_hosts(self) -> list[dict]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT h.id, h.uuid, h.name, h.status, h.cluster_id, "
                    "h.pod_id, h.data_center_id, c.name as cluster_name, "
                    "dc.name as zone_name "
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
                    "SELECT uvd.name, uvd.value FROM user_vm_details uvd "
                    "JOIN vm_instance vi ON uvd.vm_id = vi.id "
                    "WHERE vi.uuid = %s",
                    (vm_uuid,),
                )
                return cur.fetchall()

    def test_connection(self) -> bool:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
            return True
        except Exception as e:
            log.error(f"CloudStack DB connection failed: {e}")
            return False
