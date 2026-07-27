# Proxmox adopt-existing extension research spike

> **NO-GO for production:** CloudStack 4.22.1 has no supported adoption API. This prototype demonstrates a GET-only `prepare`/`create` transaction, but an extension alone cannot provide globally atomic VMID claims or durable batch-status identity when the existing Proxmox name differs from CloudStack's instance name. CloudStack-core work is required before production use.

This directory contains a fork of Apache CloudStack `4.22.1.0`'s built-in `extensions/Proxmox/proxmox.sh` from upstream commit `348ce953a99246a756b527994f7745a7be038234`.

## Status

**Not deployed and not authorized for production use.** It is a research artifact, not a deployable importer. Testing and review cannot remove the CloudStack-core blockers described below.

The normal non-adoption actions remain based on upstream. The additional behavior is activated only when the VM external detail `adopt_existing` is exactly `true`.

## Adoption transaction

CloudStack `deployVirtualMachine` persists `externaldetails` under `External:` VM details. For an adoption request, the orchestrator must pass:

- `adopt_existing=true`
- `adopt_manifest_sha256=<64 lowercase hex characters>`
- `adopt_manifest_json=<canonical JSON manifest>`

The manifest contains the exact Proxmox cluster/node/VMID/name/state, CPU/RAM, NIC device/MAC/bridge/VLAN/IP, CloudStack network identity, and one non-CD-ROM root-disk device/volume/storage/size. CloudStack 4.22.1 External instances do not support data-volume semantics, so multiple non-CD-ROM disks fail closed.

## CloudStack-core blockers

The extension framework does not provide either invariant required for future-safe adoption:

1. **Atomic claim uniqueness.** Two concurrent or adversarial `deployVirtualMachine` requests can attempt to claim the same `(extension, cluster/host, Proxmox VMID)`. A planner-side collision check or process-local lock is not globally authoritative.
2. **Durable status identity.** The stock Proxmox `statuses` action reports Proxmox VM names, while CloudStack expects CloudStack instance identity. Existing guest names are not guaranteed to equal newly allocated CloudStack instance names. Deliberately relying on per-VM fallback polling is not a durable production contract.

A production design therefore needs a CloudStack-core adoption API/protocol that atomically reserves the external identity before lifecycle reporting and maps status independently of the existing Proxmox name. Until that exists, no sync-tool executor or production canary is permitted.

### `prepare`

On the first CloudStack start transaction, `prepare`:

1. verifies the canonical manifest hash;
2. requires an already-running, unique, non-template QEMU VM on the scheduled node;
3. checks CloudStack-planned CPU/RAM and every planned MAC/VLAN/IP;
4. checks current Proxmox name, CPU/RAM and power state;
5. checks every current Proxmox NIC and guest-agent IP;
6. requires exactly one non-CD-ROM root disk and verifies its storage is active/enabled; and
7. returns the existing VMID as `details.proxmox_vmid`.

It uses GET requests only.

### `create`

`create` repeats the complete validation at the write boundary, then returns success without making a Proxmox mutation. A change between `prepare` and `create` therefore fails closed.

### `delete`

For adopted instances, `delete` is deliberately metadata-only and returns success without contacting Proxmox. This allows CloudStack transaction rollback or later CloudStack record removal without deleting the pre-existing guest. The EXIT cleanup trap also refuses deletion in adoption mode.

Normal non-adoption instances retain upstream create/start/stop/reboot/delete behavior.

## Required orchestration safeguards

A caller is not safe merely because this script exists. It must also:

- use CloudStack account `admin` in domain `ROOT` and omit `projectid`;
- use one exact CPU/RAM service offering or the configured customized offering with exact details;
- pass the exact existing IP and MAC through `iptonetworklist`;
- pin `hostid` to the unique mapped Up/Enabled External host;
- select an explicitly mapped CloudStack template/guest OS without changing the guest;
- record the returned async job and VM UUID;
- independently verify CloudStack account/domain/project, host, offering/details, NIC/IP/network and extension details;
- on failure, call CloudStack destroy/expunge only after proving the record has `adopt_existing=true`, then verify the Proxmox manifest is unchanged; and
- never issue a Proxmox stop/start/resize/reconfigure/delete as part of adoption or rollback.

## Tests

The Linux/Bash harness in `tests/test_adoption_extension.py` supplies a fake Proxmox API and records every HTTP method. Positive `prepare` and `create` controls require GET-only calls. Hash, node, planned MAC, memory, power and storage mismatches must fail with zero POST/PUT/DELETE calls. Adopted `delete` must make no API call even with a malformed manifest.

Run in a target-compatible environment:

```bash
docker run --rm -v "$PWD:/work:ro" -w /work ubuntu:24.04 bash -lc \
  'apt-get update -qq && apt-get install -y -qq python3 jq >/dev/null && \
   python3 -m unittest discover -s cloudstack-extension/Proxmox/tests -v'
```

Also run:

```bash
bash -n cloudstack-extension/Proxmox/proxmox.sh
shellcheck cloudstack-extension/Proxmox/proxmox.sh
```
