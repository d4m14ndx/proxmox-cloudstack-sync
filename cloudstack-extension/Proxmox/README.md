# Source-free Proxmox adopt-existing extension

This directory contains a custom external deployment artifact based on Apache CloudStack `4.22.1.0`'s built-in `extensions/Proxmox/proxmox.sh` at upstream commit `348ce953a99246a756b527994f7745a7be038234`.

**It does not patch or rebuild Apache CloudStack.** CloudStack continues to invoke an external executable through the supported Extensions framework.

## Status

The implementation and deterministic harness are functional, but nothing here has been deployed or authorized in production. The application-side deployment executor remains deliberately disabled. Production requires an independently reviewed exact head, management-server staging, shared-registry availability testing, and one explicitly approved canary.

## Architecture

CloudStack 4.22.1 does not natively import an existing External/Proxmox guest. The missing contracts are supplied outside CloudStack core:

1. `proxmox-cloudstack-sync` creates one durable claim with a database uniqueness constraint on `(Proxmox cluster, VMID)`.
2. A random one-time claim nonce is returned once and persisted only as a SHA-256 digest.
3. Normal CloudStack `deployVirtualMachine` orchestration creates the owner, offering, VM, NIC and IP-accounting records.
4. The custom extension receives the frozen manifest and claim credentials through External VM details.
5. After GET-only Proxmox validation, `prepare` atomically binds the claim to the CloudStack VM reference and instance name. A competing VM loses the compare-and-set.
6. `create` repeats the complete validation and idempotently validates the same binding.
7. Host `statuses` asks the authenticated registry for VMID → CloudStack instance-name mappings, so an existing Proxmox name need not match CloudStack's allocated name.

The registry must be a single authoritative service/database for all CloudStack management servers. Separate per-server SQLite databases are not sufficient. A single sidecar instance with durable storage is valid for staging; HA requires every instance to share the same SQL claim table.

## Required External VM details

An adoption deployment carries:

- `adopt_existing=true`
- `adopt_claim_id=<UUID>`
- `adopt_claim_nonce=<one-time random value>`
- `adopt_manifest_sha256=<64 lowercase hex characters>`
- `adopt_manifest_json=<canonical JSON manifest>`
- `proxmox_cluster=<canonical sidecar cluster identity>`

The nonce is sensitive transient orchestration data. Do not log it or include it in tickets. The registry stores only its digest.

The host External details must also contain the same non-secret `proxmox_cluster` value so the batch `statuses` action can obtain the correct mapping.

## Extension-to-registry configuration

The extension reads these process environment variables:

- `ADOPTION_REGISTRY_URL` — required HTTPS base URL;
- `ADOPTION_REGISTRY_HEADER_FILE` — required root-readable curl header file;
- `ADOPTION_REGISTRY_CA_FILE` — optional private CA bundle.

The header file contains one line and should be mode `0600`:

```text
X-Adoption-Registry-Token: <minimum-32-character-internal-token>
```

The same token is supplied to the sidecar only through `SYNC_ADOPTION_REGISTRY_INTERNAL_TOKEN`. Do not place the token in CloudStack host/extension details or command-line arguments.

Use a root-owned wrapper or systemd environment file to export these variables before executing `proxmox.sh`. Pin the exact script and wrapper checksums on every active management server.

## Adoption validation

Both `prepare` and `create`:

1. verify canonical manifest JSON and its SHA-256;
2. require one unique, already-running, non-template QEMU VM at the exact node and VMID;
3. compare CloudStack-planned CPU/RAM to the live guest;
4. compare every planned MAC/VLAN/IP to the manifest;
5. verify every live NIC device, MAC, bridge, VLAN and guest-agent IP;
6. verify every non-CD-ROM disk device, volume, storage and size;
7. verify every referenced Proxmox storage is active and enabled; and
8. bind or idempotently validate the unique registry claim.

Proxmox calls in successful adoption `prepare` and `create` are GET-only. A registry bind is an HTTPS POST to the sidecar, not a Proxmox mutation.

## Opaque multi-disk contract

CloudStack External instances do not expose native CloudStack data-volume semantics. Existing guests may nevertheless have multiple disks when all disks match the frozen manifest.

For adopted VMs:

- disks remain wholly Proxmox-managed;
- no fake CloudStack volume rows are created;
- attach, detach, resize, migrate and CloudStack storage accounting are unsupported;
- snapshot create/restore/delete actions fail closed;
- whole-VM start, stop, reboot, status and console remain the intended lifecycle surface; and
- any disk topology drift blocks revalidation and must be reviewed outside CloudStack.

This matches the useful behavior of the original sync tool without claiming storage capabilities it never had.

## Delete and rollback safety

For a VM carrying `adopt_existing=true`:

- `delete` is metadata-only and makes no Proxmox request;
- the EXIT cleanup trap cannot delete the existing guest;
- malformed adoption metadata biases toward retaining the guest; and
- rollback may remove only newly created CloudStack metadata after claim/VM identity is proven.

After any failed canary transaction, verify the Proxmox manifest and task history are unchanged.

## Fail-closed behavior

Adoption fails when:

- the registry is unavailable or its TLS/authentication fails;
- the claim is absent, nonce is wrong, manifest differs, or another CloudStack VM already won;
- host/VMID/name/power/CPU/RAM/NIC/IP/disk/storage evidence differs; or
- CloudStack-planned networking differs from the existing guest.

Batch `statuses` also fails rather than returning potentially wrong names when the authoritative registry is unavailable. Per-VM VMID status remains available to CloudStack through the normal `status` action.

## Tests

The Linux harness uses fake Proxmox and registry endpoints. It verifies:

- GET-only Proxmox adoption validation;
- registry binding in `prepare` and `create`;
- conflicting claims fail without Proxmox mutation;
- existing Proxmox names map to different CloudStack instance names by VMID;
- multiple exact disks are accepted as opaque topology;
- disk/resource mismatches fail closed;
- adopted delete is metadata-only; and
- adopted snapshot mutations are rejected.

Run:

```bash
docker run --rm -v "$PWD:/work:ro" -w /work ubuntu:24.04 bash -lc \
  'apt-get update -qq && apt-get install -y -qq python3 jq >/dev/null && \
   python3 -m unittest discover -s cloudstack-extension/Proxmox/tests -v'

bash -n cloudstack-extension/Proxmox/proxmox.sh
shellcheck cloudstack-extension/Proxmox/proxmox.sh
```

Passing tests establish source behavior only. They do not establish deployed checksums, registry HA, CloudStack metadata correctness, or canary safety.
