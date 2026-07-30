# Proxmox-CloudStack Sync

Keeps Apache CloudStack in sync with Proxmox VE when HA/DRS moves VMs between hosts. Provides a web dashboard for viewing VM state across both platforms, detecting drift, and reconciling differences directly in the CloudStack database.

## Features

- **Multi-cluster polling** - monitors multiple Proxmox clusters with host failover (if one node is down, tries the next)
- **Host mapping** - maps Proxmox short hostnames (e.g., `pve1`) to CloudStack FQDNs (e.g., `pve1.example.com`)
- **Drift detection** - preserves an established mutual VMID relationship across a host move only when both the CloudStack-reported source host and current Proxmox destination have globally unique mappings, then targets the destination mapping while refusing incomplete placement
- **Direct DB reconciliation** - fixes drift by updating the CloudStack database directly (required for the Extensions framework where `reconnectHost` doesn't trigger VM re-scanning)
- **Auto-reconcile mode** - optionally fix all drift automatically on each sync cycle
- **VM matching** - matches only current non-template QEMU guests to current CloudStack External records using External VMID plus a canonical, globally bijective cluster/node ↔ CloudStack host-name/ID mapping; only exactly empty-host stopped/error External rows may use the inventory-only VMID+exact-name fallback, while whitespace, populated-but-unmapped, and ambiguous hosts fail closed
- **Adoption preflight** - captures current guest type/template, CPU/RAM, NIC/IP and storage identity and returns fail-closed plans/blockers from `/api/adoption/candidates`; plans are fixed to ROOT-domain `admin` with no project, require a unique exact static or configured customized service offering, verify live Up/Enabled External host identity, reject allocated/out-of-range MAC/IP identities, and hash the non-secret resource manifest
- **Source-free adoption registry** - reserves a globally unique Proxmox cluster+VMID claim, binds it atomically to one CloudStack VM transaction, and supplies VMID-to-instance-name status identity to a separately deployed custom extension without patching CloudStack source
- **Legacy VM registration guard** - the incomplete direct-DB registration/repair endpoints are permanently unavailable while an orchestrated adopt-existing workflow is developed
- **NIC management** - captures every current Proxmox guest's NICs (MAC, bridge, VLAN tag, IP), maps Proxmox bridges/VLANs to CloudStack networks, and limits drift/reconciliation to current-cycle snapshots for mutually consistent QEMU/External pairs with complete globally unique source and destination host mappings; hostless fallback associations are never write-eligible
- **Activity log** - tracks host migrations, state changes, reconciliations, and sync events
- **Web dashboard** - filterable/searchable tables, host mapping UI, drift alerts, summary stats

## Why direct database updates?

CloudStack's Extensions framework (used for Proxmox hypervisors since 4.21) calls a `statuses` action per-host to discover VM power states. When Proxmox HA/DRS moves a VM to a different host, CloudStack never finds out because it only polls the original host. The standard `reconnectHost` API doesn't trigger the extensions framework to re-scan, and `importUnmanagedInstance` doesn't work with external hypervisors. The only reliable way to update VM placement is to write directly to the `cloud.vm_instance` table.

## Quick Start

### Docker (recommended)

```bash
cp config.example.json config.json
# Edit config.json with your Proxmox, CloudStack, and CloudStack DB credentials

docker compose up -d
```

The UI is at `http://localhost:8088`.

### Docker build only

```bash
docker build -t proxmox-cs-sync .
docker run -d \
  -p 8088:8088 \
  -v $(pwd)/config.json:/app/config.json:ro \
  -v sync-data:/app/data \
  -e SYNC_DATABASE_URL=sqlite:////app/data/sync.db \
  proxmox-cs-sync
```

### Without Docker

```bash
cp config.example.json config.json
# Edit config.json

./run.sh
# Or manually:
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

## Configuration

Copy `config.example.json` to `config.json`:

```json
{
  "database_url": "sqlite:///./sync.db",
  "sync_interval_seconds": 300,
  "auto_reconcile": false,
  "api_auth_token": "",
  "proxmox_clusters": [
    {
      "name": "prod-cluster-1",
      "hosts": ["10.0.0.10", "10.0.0.11", "10.0.0.12"],
      "user": "root@pam",
      "token_name": "sync-token",
      "token_value": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
      "verify_ssl": false
    }
  ],
  "cloudstack": {
    "url": "http://cloudstack.local:8080/client/api",
    "api_key": "your-api-key",
    "secret_key": "your-secret-key"
  },
  "cloudstack_db": {
    "host": "cloudstack-db.local",
    "port": 3306,
    "user": "cloud",
    "password": "your-db-password",
    "database": "cloud"
  }
}
```

### Proxmox clusters

Each cluster entry supports:

| Field | Description |
|-------|-------------|
| `name` | Friendly name for the cluster |
| `hosts` | List of node IPs/hostnames - tries each in order until one responds |
| `host` | Single host (backwards-compatible alternative to `hosts`) |
| `user` | Proxmox user (default: `root@pam`) |
| `token_name` | API token name |
| `token_value` | API token value |
| `password` | Alternative to token auth |
| `verify_ssl` | Verify TLS certs (default: `false`) |

### CloudStack API

Used for reading VM and host lists from CloudStack.

| Field | Description |
|-------|-------------|
| `url` | CloudStack API endpoint |
| `api_key` | API key |
| `secret_key` | Secret key |

### CloudStack Database

Required for reviewed drift reconciliation. This connects directly to the CloudStack MySQL/MariaDB `cloud` database. The legacy direct-DB VM registration and generic repair endpoints are permanently unavailable; no configuration switch can re-enable them.

| Field | Description |
|-------|-------------|
| `host` | Database hostname |
| `port` | Database port (default: `3306`) |
| `user` | Database user (default: `cloud`) |
| `password` | Database password |
| `database` | Database name (default: `cloud`) |
| `connect_timeout_seconds` | TCP and MySQL handshake timeout (default: `30`) |
| `read_timeout_seconds` | MySQL socket read timeout (default: `30`) |
| `write_timeout_seconds` | MySQL socket write timeout (default: `30`) |
| `reconnect_backoff_seconds` | Minimum delay after a failed DB probe before another scheduled/manual sync may retry (default: `30`, range: `5`–`300`) |

All three MySQL timeout values are validated in the range `1`–`120` seconds.

### Operator authentication

Every mutation, manual sync, detailed Proxmox/CloudStack inventory query, drift/log query, direct CloudStack DB query, and CloudStack topology query requires an operator token. When `api_auth_token` is empty, those routes fail closed with HTTP 503. Generate a local token and place it in `config.json` or `SYNC_API_AUTH_TOKEN`:

```bash
openssl rand -hex 32
```

The dashboard's **Operator Login** button keeps the token in browser `sessionStorage` for the current tab only. API clients may send either `X-API-Key: <token>` or `Authorization: Bearer <token>`. The token must contain at least 32 characters.

### Auto-reconcile

Set `"auto_reconcile": true` to automatically fix all detected drift on every sync cycle. When disabled (default), drift is only reported and must be fixed manually via the dashboard.

### NIC management

The app captures config-derived NIC and disk identity for every current Proxmox guest. It clears and re-establishes separate `config_current` and `nics_current` markers on every collection cycle. NIC drift and every manual or automatic NIC reconciliation path require both markers, so a failed, disabled, or restarted collection cycle cannot write from retained snapshots.

| Setting | Description |
|---------|-------------|
| `nic_sync_enabled` | Capture Proxmox NIC/storage identity for current guests, enrich running unmatched QEMU candidate IPs read-only from the guest agent, and capture CloudStack NICs for current matched External VMs (default: `true`) |
| `auto_reconcile_nics` | Automatically write NIC drift into the CloudStack DB on every sync cycle (default: `false`) |

Workflow:

1. **Map networks** - On the **Networks** tab, map each Proxmox bridge (+ optional VLAN tag) to a CloudStack network. Bridges are auto-discovered from synced VM NICs.
2. **Review** - The **NICs** tab shows a per-VM Proxmox-vs-CloudStack NIC comparison and a NIC-drift list.
3. **Reconcile** - Click "Fix in DB" per NIC, or "Reconcile All NICs", to insert/update/remove `nics` rows. IPs come from the VM (LXC config or QEMU guest agent); netmask/gateway come from the mapped CloudStack network.

Before any NIC write, the API re-derives drift from current-cycle snapshots and rejects stale or caller-manufactured payloads. `POST /api/reconcile/nics-all?dry_run=true` previews the SQL without writing. `auto_reconcile_nics` also receives zero drift when either side is stale. This does **not** make incomplete direct-DB VM adoption safe: CloudStack IP-pool/capacity accounting and complete VM/volume orchestration remain outside this compatibility path.

### Adoption planning and source-free claim registry

Adoption planning is disabled unless `adoption_policy.enabled` is true. When enabled, startup validation requires the ROOT domain UUID and one customized service-offering UUID. The account is fixed to CloudStack `admin`; project ownership is not configurable and every plan emits `project_id: null`. Enabling the executor additionally requires one explicit External template UUID; no arbitrary template is selected at runtime.

```json
"adoption_policy": {
  "enabled": true,
  "account": "admin",
  "domain_id": "ROOT-domain-uuid",
  "customized_service_offering_id": "customized-offering-uuid",
  "template_id": "external-adoption-template-uuid"
}
```

`GET /api/adoption/candidates` is always read-only. When the executor is disabled, otherwise valid candidates carry `adoption_executor_not_enabled`; when enabled, valid candidates have disposition `ready`. Candidate planning never calls `deployVirtualMachine`, `importUnmanagedInstance`, Proxmox mutation APIs, or the retired direct-DB registration helpers. For every candidate it independently requires:

- successful current-process inventory and NIC/config collection;
- one canonical globally bijective host mapping whose live CloudStack External host is uniquely `Up` and `Enabled`;
- one canonical bridge/VLAN mapping to a unique live CloudStack network;
- unique MACs that are absent from existing CloudStack NICs;
- resolved guest IPv4 addresses that are unallocated and, for CloudStack-managed IPAM, inside a configured guest IP range; an exact live ROOT/admin L2 VLAN may instead use explicitly frozen external IPAM when its name, VLAN, zone, domain, state and deployability all match;
- complete unique non-CD-ROM disk device/volume/storage/size identity;
- one unique exact static CPU/RAM offering, or the configured customized offering plus exact `cpuNumber` and `memory` details; and
- a SHA-256 manifest over placement, VMID/name, CPU/RAM, NICs and storage; and, when execution is enabled,
- an exact live host zone/cluster chain and a ready External template bound to the same CloudStack extension UUID as that cluster.

Any failed catalog lookup or ambiguous/malformed identity suppresses the manifest. The manifest is planning evidence only. When `adoption_registry_enabled` is true, `POST /api/adoption/claims` reserves the exact current candidate and caller-supplied manifest hash but never deploys anything.

The registry closes the two identity gaps outside CloudStack core:

- a database uniqueness constraint permits one durable `(Proxmox cluster, VMID)` claim;
- a non-secret monotonically increasing generation fences stale retries without placing bearer credentials in CloudStack details or payload files;
- the custom extension atomically compare-and-set binds the claim to one CloudStack VM reference and instance name; and
- host batch status obtains VMID → CloudStack instance-name mappings from the authenticated registry instead of relying on mutable Proxmox names.

Claims move through `reserved -> bound -> managed -> retiring -> released`, with a transient `managed -> operating -> managed` fence around each supported mutation. Failed pre-bind metadata creation instead uses `reserved -> cleanup -> released`: cleanup authorization atomically consumes the unbound reservation so a concurrent bind cannot succeed before metadata deletion, and release still requires authoritative CloudStack VM absence. Adoption remains non-mutating while reserved or bound: `prepare`, `create`, and the initial start acknowledgement use only Proxmox GETs and require the live VM to match the exact CPU/RAM, NIC/IP, disk and placement manifest. `POST /api/adoption/claims/{id}/activate` changes a bound claim to managed only after a fresh CloudStack API lookup independently verifies the exact VM UUID/internal instance name, External hypervisor/host, running state, owner/domain/no-project scope, CPU/RAM and Proxmox VMID.

The executor is disabled by default. When `adoption_executor_enabled` is true, `POST /api/adoption/claims/{id}/execute` rebuilds the complete live plan, compares it to the reserved manifest, and creates one immutable `adoption_executions` row. Its UUID is also passed to CloudStack as `customid`, providing a deterministic VM UUID for lost-response reconciliation. The executor first submits `deployVirtualMachine` with `startvm=false`, exact host/zone/template/offering/network/MAC/details, no project, and an exact IP for CloudStack-managed IPAM. For an explicitly frozen L2 external-IPAM network it omits the CloudStack IP allocation request while retaining the inherited IP in the immutable manifest; CloudStack NIC verification then requires exact device/network/MAC identity and rejects any different non-empty IP. Only after that job succeeds and the stopped CloudStack VM verifies exactly does it submit a separately tracked `startVirtualMachine`; those extension callbacks validate and bind the already-running Proxmox guest without mutating it. APScheduler advances active executions under a database lease, making restart and multi-instance recovery idempotent.

Ambiguous deploy/start responses are observed but never replayed automatically. `POST /api/adoption/executions/{id}/retry` is the explicit recovery action: it queries the deterministic UUID first, then performs a compare-and-set back to the appropriate pre-submit state only when retry is safe. Failed pre-bind metadata creation enters `cleanup_required`. `POST /api/adoption/executions/{id}/cleanup` permits CloudStack metadata expunge only when the exact deterministic VM is still `Stopped`, the claim remains `reserved` and unbound, and the authenticated extension obtains a one-execution cleanup authorization. Cleanup never invokes a Proxmox mutation, never replays an ambiguous destroy, and releases the claim only after the VM UUID is proven absent. Bound, running, ambiguous, or mismatched records are retained for reconciliation instead of being auto-destroyed.

Once managed, each adopted console, power, or snapshot operation revalidates the complete claim identity and atomically acquires an exact operation lease before the Proxmox request. The lease changes the claim to `operating`, blocks retirement and concurrent operations, and is completed back to `managed` only after the full Proxmox task sequence succeeds. Ambiguous failures retain the fence until its two-hour expiry; retirement may reclaim only the exact expired lease UUID. Console access creates a leased Proxmox VNC proxy ticket and is rejected before activation. Start, stop and reboot use the normal Proxmox task APIs. Snapshot list/create/restore/delete use Proxmox VM snapshot custom actions; they are not represented as native CloudStack data-volume snapshots. The frozen manifest remains adoption audit evidence rather than a permanent post-adoption configuration lock.

Existing disks remain opaque, Proxmox-managed topology. No CloudStack volume row or native volume-lifecycle claim is fabricated, so disk resize/attach/detach and migration remain unsupported. CPU/RAM resize also remains unsupported because the External provider does not currently coordinate a Proxmox change with CloudStack service-offering and capacity state; a Proxmox-only custom action would create incorrect CloudStack metadata. Adopted delete remains metadata-only: it retains the guest and moves the claim to a non-reusable `retiring` tombstone that stays in status mappings. The operator-authenticated finalizer releases it only after a fresh `listVirtualMachines id=<bound UUID>` query returns no CloudStack VM.

Production wiring requires CloudStack 4.22.0.1 or later, denied-user protection for every Proxmox/adoption routing detail listed in the extension README, the reviewed custom extension on every management server, an HTTPS registry endpoint, the registry token in a root-readable curl header file, host external details `proxmox_cluster` and `adoption_status_registry_required=true`, a shared MariaDB registry, an explicit External template UUID, and a separately approved canary. Enabling the executor or invoking execute/retry/cleanup is an operator decision; source installation alone does not adopt a guest. Legacy direct-DB registration remains unavailable.

### Environment overrides

| Variable | Description |
|----------|-------------|
| `SYNC_CONFIG` | Path to config file (default: `config.json`) |
| `SYNC_DATABASE_URL` | Override database URL |
| `SYNC_SYNC_INTERVAL_SECONDS` | Override sync interval |
| `SYNC_API_AUTH_TOKEN` | Operator token override (minimum 32 characters) |
| `SYNC_ADOPTION_REGISTRY_INTERNAL_TOKEN` | Separate extension-to-registry token (minimum 32 characters; required when registry is enabled) |
| `SYNC_ADOPTION_EXECUTOR_ENABLED` | Enable durable adoption execution; requires policy, registry and template ID (default `false`) |
| `SYNC_ADOPTION_EXECUTOR_INTERVAL_SECONDS` | Active execution polling interval (5–300 seconds; default `10`) |
| `SYNC_ADOPTION_EXECUTOR_LEASE_SECONDS` | Database worker lease (30–600 seconds; default `60`) |

### Creating a Proxmox API token

```bash
pveum user token add root@pam sync-token --privsep=0
```

The `--privsep=0` flag gives the token the same permissions as the user. For a least-privilege setup, create a dedicated user with `VM.Audit` and `Sys.Audit` on `/`.

## Workflow

1. **Map hosts** - Go to the Hosts tab and map each Proxmox node to its CloudStack host (required because Proxmox uses short hostnames while CloudStack uses FQDNs)
2. **Sync** - The app polls Proxmox clusters on schedule and syncs VM state to the local database
3. **Match VMs** - only current non-template QEMU guests and current CloudStack External rows are considered. Automatic matching requires the External `proxmox_vmid`, exact mapped cluster/node identity, and uniqueness in both directions, with a unique VMID+exact-name fallback for hostless stopped/error rows. Manual links are retained only while VMID and mapped placement remain authoritative. VMware/KVM name matches are not accepted.
4. **Detect drift** - The Drift tab shows VMs where Proxmox reality doesn't match CloudStack's records
5. **Reconcile** - Click "Fix in DB" per VM or "Reconcile All" to update the CloudStack database directly
6. **Preflight adoption** - review `/api/adoption/candidates`, reserve one exact manifest through `/api/adoption/claims`, then use the independently reviewed extension-assisted transaction. Legacy direct-DB registration remains permanently unavailable.

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/dashboard` | GET | Summary stats |
| `/api/status` | GET | Sync status and config info |
| `/api/sync` | POST | Trigger immediate sync |
| `/api/proxmox/vms` | GET | List Proxmox VMs (filterable) |
| `/api/proxmox/clusters` | GET | List discovered clusters |
| `/api/adoption/candidates` | GET | Current read-only QEMU/LXC/template adoption dispositions and blockers |
| `/api/adoption/claims` | POST | Reserve one exact current cluster+VMID claim; does not deploy or mutate CloudStack/Proxmox |
| `/api/adoption/claims` | GET | List secret-free claim state |
| `/api/adoption/claims/{id}/execute` | POST | Create/resume one immutable execution and submit at most its next fenced CloudStack step |
| `/api/adoption/executions` | GET | List secret-free execution state and job IDs |
| `/api/adoption/executions/{id}` | GET | Read one secret-free execution status |
| `/api/adoption/executions/{id}/reconcile` | POST | Observe/advance at most one already-authorized execution step |
| `/api/adoption/executions/{id}/retry` | POST | Explicitly retry an ambiguous deploy/start after deterministic UUID revalidation |
| `/api/adoption/executions/{id}/cleanup` | POST | Explicit exact stopped/unbound CloudStack metadata rollback |
| `/api/adoption/claims/{id}/activate` | POST | Operator-authenticated exact CloudStack verification and atomic `bound` → `managed` transition |
| `/api/internal/adoption/claims/{id}/bind` | POST | Extension-authenticated atomic claim bind/idempotent validation |
| `/api/internal/adoption/claims/{id}/authorize-cleanup-delete` | POST | Authorize only the exact executor-owned metadata-only rollback delete |
| `/api/internal/adoption/claims/{id}/lifecycle-state` | POST | Extension-authenticated complete identity check and current claim lifecycle state |
| `/api/internal/adoption/claims/{id}/lifecycle-lease` | POST | Atomically fence one exact managed mutation against retirement |
| `/api/internal/adoption/claims/{id}/lifecycle-lease/complete` | POST | Complete only the matching lease UUID and return the claim to managed |
| `/api/internal/adoption/claims/{id}/retire` | POST | Extension-authenticated non-reusable metadata-delete tombstone |
| `/api/internal/adoption/status-map` | GET | Extension-authenticated VMID → CloudStack instance-name map |
| `/api/adoption/claims/{id}/finalize-release` | POST | Operator-authenticated release after server-side CloudStack UUID absence verification |
| `/api/cloudstack/vms` | GET | List CloudStack VMs |
| `/api/cloudstack/hosts` | GET | List CloudStack hosts (from API) |
| `/api/cloudstack/db-hosts` | GET | List CloudStack hosts (from DB, with zone/cluster) |
| `/api/cloudstack/db-accounts` | GET | List CloudStack accounts (from DB) |
| `/api/cloudstack/db-service-offerings` | GET | List service offerings (from DB) |
| `/api/cloudstack/db-guest-os` | GET | List guest OS types (from DB) |
| `/api/drift` | GET | Detect host/state mismatches |
| `/api/reconcile/vm` | POST | Fix a single drifted VM in CloudStack DB |
| `/api/reconcile/all` | POST | Fix all drifted VMs in CloudStack DB |
| `/api/reconcile/status` | GET | Check if CloudStack DB is configured |
| `/api/register` | POST | Permanently returns `410 Gone`; direct-DB registration was removed |
| `/api/cloudstack/repair-vm/{uuid}` | POST | Permanently returns `410 Gone`; generic direct-DB repair was removed |
| `/api/match` | POST | Manually match a Proxmox VM to a CloudStack VM |
| `/api/unmatch/{id}` | POST | Remove a match |
| `/api/host-mappings` | GET | List host mappings |
| `/api/host-mappings` | POST | Create a host mapping |
| `/api/host-mappings/{id}` | DELETE | Delete a host mapping |
| `/api/nics` | GET | Per-VM Proxmox-vs-CloudStack NIC comparison |
| `/api/nics/drift` | GET | Detect NIC mismatches (missing/extra/network/IP) |
| `/api/network-mappings` | GET/POST | List / create bridge+VLAN → network mappings |
| `/api/network-mappings/{id}` | DELETE | Delete a network mapping |
| `/api/network-mappings/proxmox-bridges` | GET | Discovered Proxmox bridges/VLANs |
| `/api/cloudstack/db-networks` | GET | List CloudStack networks (from DB) |
| `/api/reconcile/nic` | POST | Fix one NIC in the CloudStack DB (`dry_run` supported) |
| `/api/reconcile/nics-all` | POST | Fix all NIC drift (`?dry_run=true` to preview) |
| `/api/logs` | GET | Sync activity log |

## License

MIT
