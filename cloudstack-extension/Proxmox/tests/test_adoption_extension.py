import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "proxmox.sh"


class AdoptExtensionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("jq") is None:
            raise unittest.SkipTest("jq is required for extension tests")

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.bin = self.root / "bin"
        self.fixtures = self.root / "fixtures"
        self.bin.mkdir()
        self.fixtures.mkdir()
        self.calls = self.root / "calls.jsonl"
        self.registry_header = self.root / "registry-header"
        self.registry_header.write_text(
            "X-Adoption-Registry-Token: test-internal-token-not-real\n",
            encoding="utf-8",
        )
        self.registry_header.chmod(0o600)
        self._write_fake_curl()
        self._write_sha256sum()
        self._write_default_fixtures()

    def tearDown(self):
        self.tempdir.cleanup()

    def _write_fake_curl(self):
        path = self.bin / "curl"
        path.write_text(
            """#!/usr/bin/env python3
import json, os, pathlib, sys, urllib.parse
args=sys.argv[1:]
method='GET'
url=''
body=None
for i, value in enumerate(args):
    if value == '-X' and i + 1 < len(args):
        method=args[i+1]
    if value == '--data-binary' and i + 1 < len(args):
        source=args[i+1]
        if source.startswith('@'):
            body=json.loads(pathlib.Path(source[1:]).read_text())
    if value.startswith('https://'):
        url=value
target='registry' if 'adoption-registry.invalid' in url else 'proxmox'
if target == 'registry':
    path=url.split('adoption-registry.invalid',1)[-1]
else:
    path=url.split('/api2/json',1)[-1]
with open(os.environ['CALL_LOG'],'a',encoding='utf-8') as f:
    f.write(json.dumps({'target':target,'method':method,'path':path,'body':body})+'\\n')
fixtures=pathlib.Path(os.environ['FIXTURE_DIR'])
if target == 'registry':
    if os.environ.get('REGISTRY_FAIL') == '1':
        raise SystemExit(22)
    if method == 'POST' and '/bind' in path:
        if os.environ.get('REGISTRY_BIND_CONFLICT') == '1':
            raise SystemExit(22)
        print(json.dumps({'status':'bound','claim':{'state':'bound'}}))
        raise SystemExit(0)
    if method == 'POST' and '/release' in path:
        print(json.dumps({'status':'released','claim':{'state':'released'}}))
        raise SystemExit(0)
    if method == 'GET' and '/status-map?' in path:
        sys.stdout.write((fixtures/'status-map.json').read_text())
        raise SystemExit(0)
    raise SystemExit(22)
key={
 '/cluster/nextid':'nextid.json',
 '/cluster/resources?type=vm':'resources.json',
 '/nodes/p2-hv07/qemu':'node-vms.json',
 '/nodes/p2-hv07/qemu/114/config':'config.json',
 '/nodes/p2-hv07/qemu/114/status/current':'status.json',
 '/nodes/p2-hv07/qemu/114/agent/network-get-interfaces':'agent.json',
 '/nodes/p2-hv07/storage/ceph/status':'storage.json',
}.get(path)
if method != 'GET':
    print(json.dumps({'data':'UPID:fake'}))
    raise SystemExit(0)
if key is None or not (fixtures/key).exists():
    print(json.dumps({'error':'unmapped fixture'}))
    raise SystemExit(22)
sys.stdout.write((fixtures/key).read_text())
""",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def _write_sha256sum(self):
        path = self.bin / "sha256sum"
        path.write_text(
            """#!/usr/bin/env python3
import hashlib, sys
content=sys.stdin.buffer.read()
print(hashlib.sha256(content).hexdigest()+'  -')
""",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def _write_json(self, name, value):
        (self.fixtures / name).write_text(json.dumps(value), encoding="utf-8")

    def _write_default_fixtures(self):
        self._write_json("nextid.json", {"data": 999})
        self._write_json(
            "node-vms.json",
            {
                "data": [
                    {
                        "vmid": 114,
                        "name": "LTS-NP2-GLR01",
                        "template": 0,
                        "status": "running",
                    }
                ]
            },
        )
        self._write_json(
            "status-map.json",
            {
                "proxmox_cluster": "p2",
                "vmid_to_instance_name": {"114": "i-2-114-VM"},
                "bindings": {
                    "114": {
                        "cloudstack_instance_name": "i-2-114-VM",
                        "expected_proxmox_name": "LTS-NP2-GLR01",
                        "manifest_sha256": "0" * 64,
                    }
                },
            },
        )
        self._write_json(
            "resources.json",
            {
                "data": [
                    {
                        "vmid": 114,
                        "type": "qemu",
                        "node": "p2-hv07",
                        "template": 0,
                        "status": "running",
                    }
                ]
            },
        )
        self._write_json(
            "config.json",
            {
                "data": {
                    "name": "LTS-NP2-GLR01",
                    "template": 0,
                    "cores": 4,
                    "sockets": 1,
                    "memory": 8192,
                    "net0": "virtio=BC:24:11:AA:BB:CC,bridge=vmbr0,tag=120,firewall=1",
                    "scsi0": "ceph:vm-114-disk-0,size=90G,iothread=1",
                    "ide2": "none,media=cdrom",
                }
            },
        )
        self._write_json("status.json", {"data": {"status": "running"}})
        self._write_json(
            "agent.json",
            {
                "data": {
                    "result": [
                        {
                            "name": "Ethernet",
                            "hardware-address": "bc:24:11:aa:bb:cc",
                            "ip-addresses": [
                                {
                                    "ip-address": "10.120.0.100",
                                    "ip-address-type": "ipv4",
                                    "prefix": 24,
                                }
                            ],
                        }
                    ]
                }
            },
        )
        self._write_json("storage.json", {"data": {"active": 1, "enabled": 1}})

    @staticmethod
    def _manifest():
        return {
            "placement": {"cluster": "p2", "node": "p2-hv07"},
            "vmid": 114,
            "name": "LTS-NP2-GLR01",
            "status": "running",
            "cpus": 4,
            "memory_mib": 8192,
            "networks": [
                {
                    "device": "net0",
                    "mac": "BC:24:11:AA:BB:CC",
                    "bridge": "vmbr0",
                    "tag": 120,
                    "ip": "10.120.0.100",
                    "cloudstack_network_id": "network-uuid",
                    "cloudstack_network_name": "Canary L2",
                }
            ],
            "storage": [
                {
                    "device": "scsi0",
                    "volume": "ceph:vm-114-disk-0",
                    "storage": "ceph",
                    "size": "90G",
                }
            ],
        }

    def _payload(self, *, manifest=None, hash_override=None, vmid=None, planned_mac=None):
        manifest = manifest if manifest is not None else self._manifest()
        canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
        digest = hash_override or hashlib.sha256(canonical.encode()).hexdigest()
        details = {} if vmid is None else {"proxmox_vmid": str(vmid)}
        return {
            "virtualmachineid": "cloudstack-vm-uuid",
            "virtualmachinename": "i-2-114-VM",
            "externaldetails": {
                "extension": {
                    "url": "proxmox.invalid",
                    "user": "test@pam",
                    "token": "test-token",
                    "secret": "not-a-real-secret",
                },
                "host": {
                    "node": "p2-hv07",
                    "proxmox_cluster": "p2",
                    "verify_tls_certificate": "true",
                },
                "virtualmachine": {
                    "adopt_existing": "true",
                    "adopt_claim_id": "8f3dd2a6-ed80-4abf-8188-e09a8818bb73",
                    "adopt_claim_nonce": "test-claim-nonce-123456789012345678901234567890",
                    "adopt_manifest_sha256": digest,
                    "adopt_manifest_json": canonical,
                    "proxmox_cluster": "p2",
                },
            },
            "cloudstack.vm.details": {
                "uuid": "cloudstack-vm-uuid",
                "name": "i-2-114-VM",
                "minRam": 8192 * 1024 * 1024,
                "cpus": 4,
                "details": details,
                "nics": [
                    {
                        "mac": planned_mac or "BC:24:11:AA:BB:CC",
                        "broadcastUri": "vlan://120",
                        "ip": "10.120.0.100",
                    }
                ],
            },
        }

    def _run(self, action, payload, env_overrides=None):
        payload_path = self.root / f"{action}.json"
        payload_path.write_text(json.dumps(payload), encoding="utf-8")
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{self.bin}{os.pathsep}{env['PATH']}",
                "CALL_LOG": str(self.calls),
                "FIXTURE_DIR": str(self.fixtures),
                "ADOPTION_REGISTRY_URL": "https://adoption-registry.invalid",
                "ADOPTION_REGISTRY_HEADER_FILE": str(self.registry_header),
            }
        )
        env.update(env_overrides or {})
        return subprocess.run(
            ["bash", str(SCRIPT), action, str(payload_path), "30"],
            text=True,
            capture_output=True,
            env=env,
            timeout=20,
        )

    def _calls(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def assert_get_only(self):
        calls = self._calls()
        self.assertTrue(calls)
        proxmox_calls = [call for call in calls if call["target"] == "proxmox"]
        self.assertTrue(proxmox_calls)
        self.assertEqual({"GET"}, {call["method"] for call in proxmox_calls})

    def assert_no_mutations(self):
        self.assertFalse(
            [
                call
                for call in self._calls()
                if call["target"] == "proxmox"
                and call["method"] in {"POST", "PUT", "DELETE"}
            ]
        )

    def test_prepare_validates_exact_existing_vm_and_returns_vmid_with_gets_only(self):
        result = self._run("prepare", self._payload())
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("114", json.loads(result.stdout)["details"]["proxmox_vmid"])
        self.assert_get_only()

    def test_create_revalidates_and_makes_no_proxmox_mutation(self):
        result = self._run("create", self._payload(vmid=114))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("without Proxmox mutation", json.loads(result.stdout)["message"])
        self.assert_get_only()

    def test_delete_is_metadata_only_and_releases_the_claim(self):
        payload = self._payload(vmid=114)
        result = self._run("delete", payload)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("retained", json.loads(result.stdout)["message"])
        self.assert_no_mutations()
        self.assertEqual(
            ["/api/internal/adoption/claims/8f3dd2a6-ed80-4abf-8188-e09a8818bb73/release"],
            [
                call["path"]
                for call in self._calls()
                if call["target"] == "registry"
            ],
        )

    def test_malformed_adopted_delete_fails_without_touching_proxmox(self):
        payload = self._payload(vmid=114, hash_override="bad")
        payload["externaldetails"]["virtualmachine"]["adopt_manifest_json"] = "not-json"
        result = self._run("delete", payload)
        self.assertNotEqual(0, result.returncode)
        self.assert_no_mutations()

    def test_mismatches_fail_closed_without_mutation(self):
        cases = []

        bad_hash = self._payload(hash_override="0" * 64)
        cases.append(("hash", bad_hash))

        wrong_node_manifest = self._manifest()
        wrong_node_manifest["placement"]["node"] = "p2-hv08"
        cases.append(("node", self._payload(manifest=wrong_node_manifest)))

        cases.append(("planned-mac", self._payload(planned_mac="BC:24:11:00:00:01")))

        for name, payload in cases:
            with self.subTest(name=name):
                self.calls.unlink(missing_ok=True)
                result = self._run("create", payload)
                self.assertNotEqual(0, result.returncode)
                self.assert_no_mutations()

    def test_multiple_non_cdrom_disks_are_preserved_as_opaque_identity(self):
        payload = self._payload(vmid=114)
        manifest = json.loads(
            payload["externaldetails"]["virtualmachine"]["adopt_manifest_json"]
        )
        manifest["storage"].append(
            {
                "device": "scsi1",
                "volume": "ceph:vm-114-disk-1",
                "storage": "ceph",
                "size": "20G",
            }
        )
        canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
        details = payload["externaldetails"]["virtualmachine"]
        details["adopt_manifest_json"] = canonical
        details["adopt_manifest_sha256"] = hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest()
        config_path = self.fixtures / "config.json"
        config = json.loads(config_path.read_text())
        config["data"]["scsi1"] = "ceph:vm-114-disk-1,size=20G,iothread=1"
        config_path.write_text(json.dumps(config))

        result = self._run("create", payload)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assert_no_mutations()
        registry_binds = [
            call
            for call in self._calls()
            if call["target"] == "registry" and call["method"] == "POST"
        ]
        self.assertEqual(1, len(registry_binds))
        self.assertEqual(114, registry_binds[0]["body"]["proxmox_vmid"])
        self.assertEqual(
            "cloudstack-vm-uuid",
            registry_binds[0]["body"]["cloudstack_vm_ref"],
        )
        self.assertEqual(
            "i-2-114-VM",
            registry_binds[0]["body"]["cloudstack_instance_name"],
        )

    def test_batch_status_uses_bound_cloudstack_name_not_proxmox_name(self):
        result = self._run("statuses", self._payload(vmid=114))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        power_state = json.loads(result.stdout)["power_state"]
        self.assertEqual({"i-2-114-VM": "poweron"}, power_state)
        self.assertNotIn("LTS-NP2-GLR01", power_state)

    def test_batch_status_rejects_vmid_reuse_name_mismatch(self):
        self._write_json(
            "node-vms.json",
            {
                "data": [
                    {
                        "vmid": 114,
                        "name": "REUSED-DIFFERENT-GUEST",
                        "template": 0,
                        "status": "running",
                    }
                ]
            },
        )
        result = self._run("statuses", self._payload(vmid=114))
        self.assertNotEqual(0, result.returncode)
        self.assert_no_mutations()

    def test_registry_conflict_fails_adoption_without_proxmox_mutation(self):
        result = self._run(
            "create",
            self._payload(vmid=114),
            {"REGISTRY_BIND_CONFLICT": "1"},
        )
        self.assertNotEqual(0, result.returncode)
        self.assert_no_mutations()

    def test_opaque_adopted_disk_snapshot_mutations_are_rejected(self):
        for action in ("CreateSnapshot", "RestoreSnapshot", "DeleteSnapshot"):
            with self.subTest(action=action):
                self.calls.unlink(missing_ok=True)
                payload = self._payload(vmid=114)
                payload["parameters"] = {"snap_name": "blocked-snapshot"}
                result = self._run(action, payload)
                self.assertNotEqual(0, result.returncode)
                self.assert_no_mutations()

    def test_live_resource_mismatches_fail_closed_without_mutation(self):
        cases = {
            "memory": ("config.json", {"memory": 4096}),
            "power": ("status.json", {"status": "stopped"}),
            "storage": ("storage.json", {"active": 0, "enabled": 1}),
        }
        for name, (fixture, update) in cases.items():
            with self.subTest(name=name):
                self.calls.unlink(missing_ok=True)
                path = self.fixtures / fixture
                value = json.loads(path.read_text())
                value["data"].update(update)
                path.write_text(json.dumps(value))
                result = self._run("create", self._payload(vmid=114))
                self.assertNotEqual(0, result.returncode)
                self.assert_no_mutations()
                self._write_default_fixtures()

    def test_non_adoption_prepare_preserves_upstream_nextid_behavior(self):
        payload = self._payload()
        payload["externaldetails"]["virtualmachine"] = {}
        result = self._run("prepare", payload)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("999", json.loads(result.stdout)["details"]["proxmox_vmid"])
        self.assertEqual(
            [
                {
                    "target": "proxmox",
                    "method": "GET",
                    "path": "/cluster/nextid",
                    "body": None,
                }
            ],
            self._calls(),
        )


if __name__ == "__main__":
    unittest.main()
