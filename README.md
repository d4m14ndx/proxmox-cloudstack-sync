# Proxmox-CloudStack Sync

Keeps Apache CloudStack in sync with Proxmox VE when HA/DRS moves VMs between hosts. Provides a web dashboard for viewing VM state across both platforms, detecting drift, and reconciling differences directly in the CloudStack database.

## Features

- **Multi-cluster polling** - monitors multiple Proxmox clusters with host failover (if one node is down, tries the next)
- **Host mapping** - maps Proxmox short hostnames (e.g., `pve1`) to CloudStack FQDNs (e.g., `pve1.example.com`)
- **Drift detection** - flags when a VM's actual Proxmox host or power state doesn't match what CloudStack thinks
- **Direct DB reconciliation** - fixes drift by updating the CloudStack database directly (required for the Extensions framework where `reconnectHost` doesn't trigger VM re-scanning)
- **Auto-reconcile mode** - optionally fix all drift automatically on each sync cycle
- **VM matching** - auto-matches VMs between Proxmox and CloudStack by instance name, VM name, or display name
- **VM registration** - register unmanaged Proxmox VMs into CloudStack via direct database inserts
- **NIC management** - captures each Proxmox VM's NICs (MAC, bridge, VLAN tag, IP), maps Proxmox bridges/VLANs to CloudStack networks, detects NIC drift, and writes `nics` rows directly into CloudStack so it knows about VM network interfaces
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

Required for drift reconciliation and VM registration. This connects directly to the CloudStack MySQL/MariaDB `cloud` database.

| Field | Description |
|-------|-------------|
| `host` | Database hostname |
| `port` | Database port (default: `3306`) |
| `user` | Database user (default: `cloud`) |
| `password` | Database password |
| `database` | Database name (default: `cloud`) |

### Auto-reconcile

Set `"auto_reconcile": true` to automatically fix all detected drift on every sync cycle. When disabled (default), drift is only reported and must be fixed manually via the dashboard.

### NIC management

CloudStack VMs registered via direct DB insert have **no `nics` rows**, so CloudStack doesn't know their network interfaces exist — breaking networking features. This app captures each matched VM's Proxmox NICs and reconciles them into CloudStack's `nics` table.

| Setting | Description |
|---------|-------------|
| `nic_sync_enabled` | Capture Proxmox + CloudStack NICs for matched VMs each sync cycle (default: `true`) |
| `auto_reconcile_nics` | Automatically write NIC drift into the CloudStack DB on every sync cycle (default: `false`) |

Workflow:

1. **Map networks** - On the **Networks** tab, map each Proxmox bridge (+ optional VLAN tag) to a CloudStack network. Bridges are auto-discovered from synced VM NICs.
2. **Review** - The **NICs** tab shows a per-VM Proxmox-vs-CloudStack NIC comparison and a NIC-drift list.
3. **Reconcile** - Click "Fix in DB" per NIC, or "Reconcile All NICs", to insert/update/remove `nics` rows. IPs come from the VM (LXC config or QEMU guest agent); netmask/gateway come from the mapped CloudStack network.

**How NIC writes stay safe:** before inserting, the app introspects the live `nics` table columns and samples an existing NIC row on the target network to copy its conventions (state, strategy, reserver, broadcast/isolation URIs), making inserts resilient across CloudStack point releases. `POST /api/reconcile/nics-all?dry_run=true` previews the exact SQL without writing. CloudStack IP-pool/capacity accounting tables are intentionally not touched.

### Environment overrides

| Variable | Description |
|----------|-------------|
| `SYNC_CONFIG` | Path to config file (default: `config.json`) |
| `SYNC_DATABASE_URL` | Override database URL |
| `SYNC_SYNC_INTERVAL_SECONDS` | Override sync interval |

### Creating a Proxmox API token

```bash
pveum user token add root@pam sync-token --privsep=0
```

The `--privsep=0` flag gives the token the same permissions as the user. For a least-privilege setup, create a dedicated user with `VM.Audit` and `Sys.Audit` on `/`.

## Workflow

1. **Map hosts** - Go to the Hosts tab and map each Proxmox node to its CloudStack host (required because Proxmox uses short hostnames while CloudStack uses FQDNs)
2. **Sync** - The app polls Proxmox clusters on schedule and syncs VM state to the local database
3. **Match VMs** - VMs are auto-matched between platforms by instance name, VM name, or display name. Use the Unmatched tab for manual matching.
4. **Detect drift** - The Drift tab shows VMs where Proxmox reality doesn't match CloudStack's records
5. **Reconcile** - Click "Fix in DB" per VM or "Reconcile All" to update the CloudStack database directly
6. **Register** - Use the Unmatched tab to register Proxmox VMs that don't exist in CloudStack yet

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/dashboard` | GET | Summary stats |
| `/api/status` | GET | Sync status and config info |
| `/api/sync` | POST | Trigger immediate sync |
| `/api/proxmox/vms` | GET | List Proxmox VMs (filterable) |
| `/api/proxmox/clusters` | GET | List discovered clusters |
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
| `/api/register` | POST | Register a Proxmox VM into CloudStack DB |
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
