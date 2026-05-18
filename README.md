# Proxmox-CloudStack Sync

Keeps Apache CloudStack in sync with Proxmox VE when HA/DRS moves VMs between hosts. Provides a web dashboard for viewing VM state across both platforms, detecting drift, and importing unmanaged Proxmox VMs into CloudStack.

## Features

- **Multi-cluster polling** - monitors multiple Proxmox clusters with host failover (if one node is down, tries the next)
- **Drift detection** - flags when a VM's actual Proxmox host or power state doesn't match what CloudStack thinks
- **VM matching** - auto-matches VMs between Proxmox and CloudStack by instance name, with manual override
- **Import workflow** - import unmanaged Proxmox VMs into CloudStack via the `importUnmanagedInstance` API
- **Activity log** - tracks host migrations, state changes, and sync events
- **Web dashboard** - filterable/searchable tables, drift alerts, summary stats

## Quick Start

### Docker (recommended)

```bash
cp config.example.json config.json
# Edit config.json with your Proxmox and CloudStack credentials

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

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/dashboard` | GET | Summary stats |
| `/api/status` | GET | Sync status and config info |
| `/api/sync` | POST | Trigger immediate sync |
| `/api/proxmox/vms` | GET | List Proxmox VMs (filterable) |
| `/api/proxmox/clusters` | GET | List discovered clusters |
| `/api/cloudstack/vms` | GET | List CloudStack VMs |
| `/api/cloudstack/hosts` | GET | List CloudStack hosts |
| `/api/drift` | GET | Detect host/state mismatches |
| `/api/match` | POST | Manually match a Proxmox VM to a CloudStack VM |
| `/api/unmatch/{id}` | POST | Remove a match |
| `/api/import` | POST | Import a Proxmox VM into CloudStack |
| `/api/logs` | GET | Sync activity log |

## License

MIT
