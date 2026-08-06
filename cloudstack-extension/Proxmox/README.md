# Source-free Proxmox adopt-existing extension

This directory contains a custom external deployment artifact based on Apache CloudStack `4.22.1.0`'s built-in `extensions/Proxmox/proxmox.sh` at upstream commit `348ce953a99246a756b527994f7745a7be038234`.

**It does not patch or rebuild Apache CloudStack.** CloudStack continues to invoke an external executable through the supported Extensions framework.

## Status

The implementation, durable application-side executor, and deterministic harness are functional, but nothing here has been deployed or authorized in production. The executor remains disabled by default. Production requires an independently reviewed exact head, management-server staging, shared-registry availability testing, and one explicitly approved canary.

## Architecture

CloudStack 4.22.1 does not natively import an existing External/Proxmox guest. The missing contracts are supplied outside CloudStack core:

1. `proxmox-cloudstack-sync` creates one durable claim with a database uniqueness constraint on `(Proxmox cluster, VMID)`.
2. A monotonically increasing, non-secret claim generation fences stale orchestration retries.
3. The executor creates metadata with `deployVirtualMachine startvm=false`, verifies the exact stopped CloudStack record, then submits a separately tracked `startVirtualMachine` job that invokes the adoption callbacks.
4. The custom extension receives the frozen manifest and non-secret claim identity/generation through External VM details; registry authorization remains only in the protected local wrapper configuration.
5. After GET-only Proxmox validation, `prepare` atomically binds the claim to the CloudStack VM reference and instance name. A competing VM loses the compare-and-set.
6. `create` repeats the complete validation and idempotently validates the same binding.
7. The operator activation route independently verifies the exact CloudStack UUID, instance name, External hypervisor, running state, VMID, CPU/RAM, ROOT-domain admin ownership, absence of a project, and canonical host mapping before atomically changing `bound` to `managed`.
8. Only `managed` claims may create an adopted console ticket or perform power/snapshot mutations. Every operation revalidates the complete claim identity and atomically acquires an exact operation lease before the Proxmox request; retirement cannot cross a live lease.
9. On explicitly adoption-enabled hosts, `statuses` asks the authenticated registry for VMID → CloudStack instance-name mappings, so an existing Proxmox name need not match CloudStack's allocated name.

The registry must be a single authoritative service/database for all CloudStack management servers. Separate per-server SQLite databases are not sufficient. A single sidecar instance with durable storage is valid for staging; HA requires every instance to share the same SQL claim table.

## Required External VM details

An adoption deployment carries:

- `adopt_existing=true`
- `adopt_claim_id=<UUID>`
- `adopt_claim_generation=<positive integer>`
- `adopt_manifest_sha256=<64 lowercase hex characters>`
- `adopt_manifest_json=<canonical JSON manifest>`
- `adopt_execution_plan_sha256=<64 lowercase hex characters>` for newly created execution plans
- `adopt_ip_overrides_json=<canonical sorted JSON list>` for newly created execution plans
- `proxmox_cluster=<canonical sidecar cluster identity>`

No bearer credential is carried in CloudStack VM details. CloudStack can retain External payload files, so these details are deliberately non-secret. Registry authorization is loaded only from the root-owned local header file used by the wrapper.

### Required CloudStack detail protection

Run CloudStack 4.22.0.1 or later; this project targets 4.22.1.0. Earlier Proxmox extension releases are affected by CVE-2026-25199 because `proxmox_vmid` was user-editable. CloudStack 4.22.1 reserves that built-in detail internally, and the custom extension additionally rejects any callback VMID that differs from the immutable adoption manifest before registry or Proxmox access.

As defense in depth, preserve the existing `user.vm.denied.details` values and append all adoption routing fields:

```text
proxmox_vmid,adopt_existing,adopt_claim_id,adopt_claim_generation,adopt_manifest_sha256,adopt_manifest_json,adopt_execution_plan_sha256,adopt_ip_overrides_json,proxmox_cluster
```

Do not replace the configuration's existing defaults when appending these values. The fixed ROOT-domain `admin` ownership policy is not a substitute for protecting routing details from accidental or delegated-account edits.

The host External details must contain both:

- the same non-secret `proxmox_cluster` value; and
- `adoption_status_registry_required=true`.

The second detail is the explicit compatibility boundary. If it is absent or
`false`, `statuses` preserves the upstream non-adoption name-based behavior and
does not contact the registry. Existing ordinary Proxmox External hosts
therefore gain no sidecar dependency merely by installing this executable.

Once set to `true`, a registry outage deliberately fails the whole batch rather
than returning an incorrect name-based status for an adopted VM. Enabling this
host detail is a separately approved deployment change and requires the
registry to be operational first. The read-only planner and claim-reservation
gate require both host details to be visible through `listHosts` before they
will produce or reserve an adoption manifest.

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
4. validate an unresolved manifest NIC only with its exact execution-time canonical IP binding, then compare every planned MAC/VLAN/IP to the effective manifest/binding value;
5. verify every live NIC device, MAC, bridge, VLAN and guest-agent IP against that effective value;
6. verify every non-CD-ROM disk device, volume, storage and size;
7. verify every referenced Proxmox storage is active and enabled; and
8. bind or idempotently validate the unique registry claim.

Proxmox calls in successful adoption `prepare`, `create`, and the initial bound-state `start` callback are GET-only. A registry bind is an HTTPS POST to the sidecar, not a Proxmox mutation. The frozen manifest is immutable evidence of what CloudStack adopted; it is not a permanent configuration lock after the claim becomes managed.

## Adoption phase and managed lifecycle

The claim lifecycle is:

```text
reserved -> bound -> managed -> retiring -> released
                      |
                      +-> operating -> managed
```

- `reserved` and `bound` are adoption states. No adopted Proxmox mutation is authorized.
- `managed` is entered only through operator-authenticated, server-side CloudStack verification. It authorizes acquisition of one normal power or Proxmox VM snapshot operation lease.
- `operating` is a transient exact-UUID fence. It blocks retirement and concurrent mutations until the full Proxmox task sequence completes. Ambiguous failures retain the lease for up to two hours; retirement can reclaim only that exact expired lease.
- `retiring` and `released` never authorize lifecycle mutation.
- Registry unavailability, stale generation, identity mismatch, an existing operation lease, or any state other than `managed` fails a new mutation before a Proxmox POST, PUT, or DELETE.

The current capability matrix is:

| Operation | During adoption | After `managed` | Notes |
|---|---|---|---|
| Status | Read-only | Supported | VMID identity remains registry-backed |
| Console | Rejected | Supported | Creates a Proxmox VNC proxy ticket only under an exact managed operation lease |
| Start | GET-only acknowledgement of the already-running guest | Supported | Uses the normal Proxmox start task |
| Stop and reboot | Rejected | Supported | Uses the normal Proxmox task paths |
| List/create/restore/delete VM snapshots | Mutation rejected | Supported | Proxmox VM snapshot custom actions, not native CloudStack volume snapshots |
| CPU/RAM resize | Rejected | Unsupported | The External provider does not coordinate Proxmox resize with CloudStack service-offering/capacity state |
| Disk resize/attach/detach | Rejected | Unsupported | Imported disks have no fabricated CloudStack volume records |
| Migration | Rejected | Unsupported | Placement and claim-node reconciliation are not transactionally modeled |
| CloudStack delete/expunge | No Proxmox deletion | Metadata-only | The guest is retained and the claim is tombstoned |

CPU/RAM changes must not be exposed as a Proxmox-only custom action: doing so would leave CloudStack offering and capacity metadata wrong. They require a separately designed two-plane operation with rollback and reconciliation. The same rule applies more strongly to storage lifecycle.

## Opaque multi-disk contract

CloudStack External instances do not expose native CloudStack data-volume semantics. Existing guests may nevertheless have multiple disks when all disks match the frozen manifest.

For adopted VMs:

- disks remain wholly Proxmox-managed;
- no fake CloudStack volume rows are created;
- attach, detach, resize, migrate and CloudStack storage accounting are unsupported;
- the immutable disk manifest is revalidated throughout adoption;
- after activation, Proxmox VM snapshots are supported without claiming CloudStack data-volume semantics; and
- post-activation disk topology changes remain unsupported and must be reviewed outside CloudStack.

This matches the useful behavior of the original sync tool without claiming storage capabilities it never had.

## Delete and rollback safety

For a VM carrying `adopt_existing=true`:

- `delete` is metadata-only and makes no Proxmox request;
- `delete` changes the claim only to a non-reusable `retiring` tombstone;
- `retiring` claims remain in VMID status mappings so a CloudStack rollback keeps a valid identity;
- only the operator-authenticated sidecar finalizer may release the tombstone, and only after its own exact CloudStack VM UUID query returns no rows;
- the EXIT cleanup trap cannot delete the existing guest;
- malformed adoption metadata biases toward retaining the guest; and
- rollback may remove only newly created CloudStack metadata after claim/VM identity is proven;
- pre-bind rollback requires an exact `Stopped` deterministic CloudStack VM UUID and an execution in `cleanup_submitting`/`cleanup_authorized`/`cleanup_submitted`;
- the extension obtains one authenticated `/authorize-cleanup-delete` decision that atomically fences the exact unbound claim as non-bindable `cleanup` before acknowledging that metadata-only delete;
- the `cleanup` claim remains non-reusable until authoritative CloudStack VM absence permits `released`; and
- destroy submission ambiguity is observed for VM absence and is never automatically replayed.

After any failed canary transaction, verify the Proxmox manifest and task history are unchanged.

## Fail-closed behavior

Adoption fails when:

- the registry is unavailable or its TLS/authentication fails;
- the claim is absent, generation is stale, manifest differs, or another CloudStack VM already won;
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
- adopted delete is metadata-only;
- exact pre-bind executor cleanup is metadata-only and malformed cleanup authorization falls back to normal retirement validation;
- bound-state snapshot and power mutations are rejected;
- managed start/stop/reboot call only the exact Proxmox VM task paths;
- managed console access is VMID-bound and bracketed by an exact operation lease;
- managed snapshot create/restore/delete call only the exact Proxmox snapshot paths;
- snapshot restore state-read failure reports an error and leaves the operation lease uncompleted;
- managed lifecycle fails closed when registry state cannot be verified;
- every managed mutation is bracketed by lease acquisition and exact completion;
- lease conflict causes zero Proxmox mutations, while an ambiguous task failure leaves the lease uncompleted;
- a user-editable callback VMID cannot reroute an adopted action; and
- clearing only `adopt_existing` cannot select ordinary mutation or delete paths while adoption identity remains.

Run:

```bash
docker run --rm -v "$PWD:/work:ro" -w /work ubuntu:24.04 bash -lc \
  'apt-get update -qq && apt-get install -y -qq python3 jq >/dev/null && \
   python3 -m unittest discover -s cloudstack-extension/Proxmox/tests -v'

bash -n cloudstack-extension/Proxmox/proxmox.sh
shellcheck cloudstack-extension/Proxmox/proxmox.sh
```

Passing tests establish source behavior only. They do not establish deployed checksums, registry HA, CloudStack metadata correctness, or canary safety.
