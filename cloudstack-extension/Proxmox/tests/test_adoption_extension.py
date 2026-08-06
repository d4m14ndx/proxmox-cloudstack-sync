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
import datetime, json, os, pathlib, sys, urllib.parse
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
    if method == 'POST' and '/authorize-cleanup-delete' in path:
        if os.environ.get('REGISTRY_CLEANUP_AUTH') != '1':
            raise SystemExit(22)
        response={
            'status':'cleanup_delete_authorized',
            'execution_id':body.get('cloudstack_vm_ref'),
        }
        malformed=os.environ.get('REGISTRY_MALFORMED_CLEANUP_AUTH')
        if malformed == 'status':
            response['status']='retiring'
        elif malformed == 'id':
            response['execution_id']='00000000-0000-4000-8000-000000000000'
        print(json.dumps(response))
        raise SystemExit(0)
    if method == 'POST' and '/lifecycle-lease/complete' in path:
        print(json.dumps({
            'status':'ok',
            'state':'managed',
            'lease_id':body.get('lease_id'),
        }))
        raise SystemExit(0)
    if method == 'POST' and path.endswith('/lifecycle-lease'):
        if (
            os.environ.get('REGISTRY_LEASE_CONFLICT') == '1'
            or os.environ.get('REGISTRY_LEASE_FAIL') == '1'
        ):
            raise SystemExit(22)
        lease_response={
            'status':'operating',
            'lease_id':'22222222-2222-4222-8222-222222222222',
            'action':body.get('action'),
            'expires_at':(
                datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(hours=1)
            ).isoformat(),
        }
        malformed=os.environ.get('REGISTRY_MALFORMED_LEASE')
        if malformed == 'uuid':
            lease_response['lease_id']='x' * 36
        elif malformed == 'expiry':
            lease_response['expires_at']='not-a-time'
        elif malformed == 'expired':
            lease_response['expires_at']='2000-01-01T00:00:00+00:00'
        elif malformed == 'action':
            lease_response['action']='stop'
        elif malformed == 'status':
            lease_response['status']='managed'
        print(json.dumps(lease_response))
        raise SystemExit(0)
    if method == 'POST' and '/lifecycle-state' in path:
        expected_vmid=os.environ.get('REGISTRY_EXPECT_VMID')
        if expected_vmid is not None and str(body.get('proxmox_vmid')) != expected_vmid:
            raise SystemExit(22)
        expected_node=os.environ.get('REGISTRY_EXPECT_NODE')
        if expected_node is not None and body.get('proxmox_node') != expected_node:
            raise SystemExit(22)
        print(json.dumps({'status':'ok','state':os.environ.get('REGISTRY_CLAIM_STATE','bound')}))
        raise SystemExit(0)
    if method == 'POST' and '/retire' in path:
        print(json.dumps({'status':'retiring','claim':{'state':'retiring'}}))
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
 '/nodes/p2-hv07/qemu/114/snapshot':'snapshots.json',
 '/nodes/p2-hv07/qemu/114/agent/network-get-interfaces':'agent.json',
 '/nodes/p2-hv07/network':'node-network.json',
 '/nodes/p2-hv07/storage/ceph/status':'storage.json',
}.get(path)
if method == 'POST' and path == '/nodes/p2-hv07/qemu/114/vncproxy':
    data={'ticket':'PVE:fake-ticket'}
    if os.environ.get('VNC_MISSING_PORT') != '1':
        data['port']=5900
    print(json.dumps({'data':data}))
    raise SystemExit(0)
if method != 'GET':
    print(json.dumps({'data':'UPID:fake'}))
    raise SystemExit(0)
if '/tasks/' in path and path.endswith('/status'):
    print(json.dumps({
        'data':{
            'status':'stopped',
            'exitstatus':os.environ.get('TASK_EXIT_STATUS','OK'),
        }
    }))
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
            "node-network.json",
            {
                "data": [
                    {
                        "iface": "vmbr0",
                        "type": "eth",
                        "method": "static",
                        "address": "10.0.0.7",
                    }
                ]
            },
        )
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
        self._write_json(
            "snapshots.json",
            {"data": [{"name": "baseline", "description": "managed snapshot"}]},
        )

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
                    "ip_allocation": "cloudstack",
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

    def _payload(
        self,
        *,
        manifest=None,
        hash_override=None,
        vmid=None,
        planned_mac=None,
        planned_ip: str | None = "10.120.0.100",
    ):
        manifest = manifest if manifest is not None else self._manifest()
        canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
        digest = hash_override or hashlib.sha256(canonical.encode()).hexdigest()
        details = {} if vmid is None else {"proxmox_vmid": str(vmid)}
        planned_nic = {
            "mac": planned_mac or "BC:24:11:AA:BB:CC",
            "broadcastUri": "vlan://120",
        }
        if planned_ip is not None:
            planned_nic["ip"] = planned_ip
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
                    "adoption_status_registry_required": "true",
                    "verify_tls_certificate": "true",
                },
                "virtualmachine": {
                    "adopt_existing": "true",
                    "adopt_claim_id": "8f3dd2a6-ed80-4abf-8188-e09a8818bb73",
                    "adopt_claim_generation": 1,
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
                "nics": [planned_nic],
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

    def test_prepare_accepts_exact_operator_ip_for_explicitly_unresolved_nic(self):
        manifest = self._manifest()
        manifest["networks"][0].update({
            "ip": None,
            "ip_allocation": "external",
            "ip_override_required": True,
        })
        payload = self._payload(manifest=manifest, planned_ip=None)
        payload["externaldetails"]["virtualmachine"].update({
            "adopt_execution_plan_sha256": "a" * 64,
            "adopt_ip_overrides_json": (
                '[{"device_id":0,"ip":"103.153.30.149"}]'
            ),
        })

        result = self._run("prepare", payload)

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        proxmox_paths = [
            call["path"]
            for call in self._calls()
            if call["target"] == "proxmox"
        ]
        self.assertNotIn(
            "/nodes/p2-hv07/qemu/114/agent/network-get-interfaces",
            proxmox_paths,
        )
        bind = next(
            call
            for call in self._calls()
            if call["target"] == "registry" and call["path"].endswith("/bind")
        )
        self.assertEqual("a" * 64, bind["body"]["execution_plan_sha256"])
        self.assertEqual(
            '[{"device_id":0,"ip":"103.153.30.149"}]',
            bind["body"]["ip_overrides_json"],
        )

    def test_prepare_rejects_unresolved_nic_without_execution_binding(self):
        manifest = self._manifest()
        manifest["networks"][0].update({
            "ip": None,
            "ip_allocation": "external",
            "ip_override_required": True,
        })

        result = self._run(
            "prepare",
            self._payload(manifest=manifest, planned_ip=None),
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("requires execution binding", result.stdout.lower())
        self.assert_no_mutations()

    def test_prepare_accepts_absent_or_exact_cloudstack_ip_for_external_ipam(self):
        for planned_ip in (None, "10.120.0.100"):
            with self.subTest(planned_ip=planned_ip):
                manifest = self._manifest()
                manifest["networks"][0]["ip_allocation"] = "external"
                result = self._run(
                    "prepare",
                    self._payload(manifest=manifest, planned_ip=planned_ip),
                )
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assert_get_only()

    def test_prepare_ignores_guest_agent_identity_for_external_ipam(self):
        manifest = self._manifest()
        manifest["networks"][0]["ip_allocation"] = "external"
        self._write_json("agent.json", {"data": {"result": []}})

        result = self._run(
            "prepare",
            self._payload(manifest=manifest, planned_ip=None),
        )

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        proxmox_paths = [
            call["path"]
            for call in self._calls()
            if call["target"] == "proxmox"
        ]
        self.assertNotIn(
            "/nodes/p2-hv07/qemu/114/agent/network-get-interfaces",
            proxmox_paths,
        )

    def test_prepare_ignores_guest_agent_identity_for_new_external_plan(self):
        manifest = self._manifest()
        manifest["networks"][0]["ip_allocation"] = "external"
        payload = self._payload(manifest=manifest, planned_ip=None)
        payload["externaldetails"]["virtualmachine"].update({
            "adopt_execution_plan_sha256": "a" * 64,
            "adopt_ip_overrides_json": "[]",
        })
        self._write_json("agent.json", {"data": {"result": []}})

        result = self._run("prepare", payload)

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        proxmox_paths = [
            call["path"]
            for call in self._calls()
            if call["target"] == "proxmox"
        ]
        self.assertNotIn(
            "/nodes/p2-hv07/qemu/114/agent/network-get-interfaces",
            proxmox_paths,
        )

    def test_prepare_keeps_guest_agent_gate_for_cloudstack_ipam(self):
        self._write_json("agent.json", {"data": {"result": []}})

        result = self._run("prepare", self._payload())

        self.assertNotEqual(0, result.returncode)
        self.assertIn("guest agent", result.stdout.lower())
        self.assert_no_mutations()

    def test_prepare_rejects_conflicting_cloudstack_ip_for_external_ipam(self):
        manifest = self._manifest()
        manifest["networks"][0]["ip_allocation"] = "external"
        result = self._run(
            "prepare",
            self._payload(manifest=manifest, planned_ip="10.120.0.200"),
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn(
            "CloudStack planned IP conflicts with external IPAM manifest",
            result.stdout + result.stderr,
        )
        self.assert_no_mutations()

    def test_prepare_rejects_unknown_ip_allocation_mode(self):
        manifest = self._manifest()
        manifest["networks"][0]["ip_allocation"] = "unmanaged"
        result = self._run("prepare", self._payload(manifest=manifest))
        self.assertNotEqual(0, result.returncode)
        self.assertIn(
            "Invalid NIC IP allocation mode",
            result.stdout + result.stderr,
        )
        self.assert_no_mutations()

    def test_create_revalidates_and_makes_no_proxmox_mutation(self):
        result = self._run("create", self._payload(vmid=114))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("without Proxmox mutation", json.loads(result.stdout)["message"])
        self.assert_get_only()

    def test_delete_is_metadata_only_and_tombstones_the_claim(self):
        payload = self._payload(vmid=114)
        result = self._run("delete", payload)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("retained", json.loads(result.stdout)["message"])
        self.assert_no_mutations()
        self.assertEqual(
            [
                "/api/internal/adoption/claims/8f3dd2a6-ed80-4abf-8188-e09a8818bb73/authorize-cleanup-delete",
                "/api/internal/adoption/claims/8f3dd2a6-ed80-4abf-8188-e09a8818bb73/retire",
            ],
            [
                call["path"]
                for call in self._calls()
                if call["target"] == "registry"
            ],
        )

    def test_explicit_cleanup_delete_is_metadata_only_and_skips_retirement(self):
        result = self._run(
            "delete",
            self._payload(vmid=114),
            {"REGISTRY_CLEANUP_AUTH": "1"},
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("rollback authorized", json.loads(result.stdout)["message"].lower())
        self.assert_no_mutations()
        registry_paths = [
            call["path"]
            for call in self._calls()
            if call["target"] == "registry"
        ]
        self.assertEqual(1, len(registry_paths))
        self.assertTrue(registry_paths[0].endswith("/authorize-cleanup-delete"))

    def test_malformed_cleanup_authorization_cannot_skip_retirement(self):
        for malformed in ("status", "id"):
            with self.subTest(malformed=malformed):
                self.calls.unlink(missing_ok=True)
                result = self._run(
                    "delete",
                    self._payload(vmid=114),
                    {
                        "REGISTRY_CLEANUP_AUTH": "1",
                        "REGISTRY_MALFORMED_CLEANUP_AUTH": malformed,
                    },
                )
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn("tombstoned", json.loads(result.stdout)["message"])
                self.assert_no_mutations()
                registry_paths = [
                    call["path"]
                    for call in self._calls()
                    if call["target"] == "registry"
                ]
                self.assertTrue(registry_paths[-1].endswith("/retire"))

    def test_malformed_adopted_delete_fails_without_touching_proxmox(self):
        payload = self._payload(vmid=114, hash_override="bad")
        payload["externaldetails"]["virtualmachine"]["adopt_manifest_json"] = "not-json"
        result = self._run("delete", payload)
        self.assertNotEqual(0, result.returncode)
        self.assertTrue(json.loads(result.stdout))
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
        self.assertEqual(1, registry_binds[0]["body"]["generation"])
        self.assertFalse(
            [key for key in registry_binds[0]["body"] if "nonce" in key.lower()]
        )
        self.assertEqual(114, registry_binds[0]["body"]["proxmox_vmid"])
        self.assertEqual(
            "cloudstack-vm-uuid",
            registry_binds[0]["body"]["cloudstack_vm_ref"],
        )
        self.assertEqual(
            "i-2-114-VM",
            registry_binds[0]["body"]["cloudstack_instance_name"],
        )

    def test_non_integer_claim_generation_fails_without_mutation(self):
        payload = self._payload(vmid=114)
        payload["externaldetails"]["virtualmachine"][
            "adopt_claim_generation"
        ] = "not-an-integer"
        result = self._run("prepare", payload)
        self.assertNotEqual(0, result.returncode)
        error = json.loads(result.stdout)
        self.assertEqual("error", error["status"])
        self.assertEqual("Invalid adoption claim generation", error["error"])
        self.assertNotIn("jq:", result.stderr)
        self.assert_no_mutations()
        self.assertFalse(
            [call for call in self._calls() if call["target"] == "registry"]
        )

    def test_bound_adoption_power_actions_remain_non_mutating(self):
        for action in ("start", "stop", "reboot"):
            with self.subTest(action=action):
                self.calls.unlink(missing_ok=True)
                result = self._run(action, self._payload(vmid=114))
                response = json.loads(result.stdout)
                if action == "start":
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    self.assertEqual("success", response["status"])
                    self.assertIn("without Proxmox mutation", response["message"])
                else:
                    self.assertNotEqual(0, result.returncode)
                    self.assertEqual("error", response["status"])
                self.assert_no_mutations()
                self.assertEqual(
                    1,
                    len(
                        [
                            call
                            for call in self._calls()
                            if call["target"] == "registry"
                            and "/lifecycle-state" in call["path"]
                        ]
                    ),
                )

    def test_managed_adoption_power_actions_mutate_exact_proxmox_vm(self):
        expected_paths = {
            "start": "/nodes/p2-hv07/qemu/114/status/start",
            "stop": "/nodes/p2-hv07/qemu/114/status/stop",
            "reboot": "/nodes/p2-hv07/qemu/114/status/reboot",
        }
        for action, expected_path in expected_paths.items():
            with self.subTest(action=action):
                self.calls.unlink(missing_ok=True)
                result = self._run(
                    action,
                    self._payload(vmid=114),
                    {"REGISTRY_CLAIM_STATE": "managed"},
                )
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertEqual("success", json.loads(result.stdout)["status"])
                mutations = [
                    call
                    for call in self._calls()
                    if call["target"] == "proxmox"
                    and call["method"] in {"POST", "PUT", "DELETE"}
                ]
                self.assertEqual([expected_path], [call["path"] for call in mutations])

    def test_adopted_console_is_managed_only_vmid_bound_and_leased(self):
        bound = self._run(
            "getconsole",
            self._payload(vmid=114),
            {"REGISTRY_CLAIM_STATE": "bound"},
        )
        self.assertNotEqual(0, bound.returncode)
        self.assert_no_mutations()

        self.calls.unlink(missing_ok=True)
        managed = self._run(
            "getconsole",
            self._payload(vmid=114),
            {"REGISTRY_CLAIM_STATE": "managed"},
        )
        self.assertEqual(0, managed.returncode, managed.stdout + managed.stderr)
        self.assertEqual("10.0.0.7", json.loads(managed.stdout)["console"]["host"])

        calls = self._calls()
        acquire_index = next(
            index
            for index, call in enumerate(calls)
            if call["target"] == "registry"
            and call["path"].endswith("/lifecycle-lease")
        )
        console_index = next(
            index
            for index, call in enumerate(calls)
            if call["target"] == "proxmox"
            and call["path"] == "/nodes/p2-hv07/qemu/114/vncproxy"
        )
        complete_index = next(
            index
            for index, call in enumerate(calls)
            if call["target"] == "registry"
            and call["path"].endswith("/lifecycle-lease/complete")
        )
        self.assertLess(acquire_index, console_index)
        self.assertLess(console_index, complete_index)
        self.assertEqual("console", calls[acquire_index]["body"]["action"])

        self.calls.unlink(missing_ok=True)
        partial = self._run(
            "getconsole",
            self._payload(vmid=114),
            {
                "REGISTRY_CLAIM_STATE": "managed",
                "VNC_MISSING_PORT": "1",
            },
        )
        self.assertNotEqual(0, partial.returncode)
        self.assertNotIn("PVE:fake-ticket", partial.stdout)
        self.assertFalse(
            [
                call
                for call in self._calls()
                if call["target"] == "registry"
                and call["path"].endswith("/lifecycle-lease/complete")
            ]
        )

        for failure_flag in ("REGISTRY_LEASE_CONFLICT", "REGISTRY_LEASE_FAIL"):
            with self.subTest(failure_flag=failure_flag):
                self.calls.unlink(missing_ok=True)
                denied = self._run(
                    "getconsole",
                    self._payload(vmid=114),
                    {
                        "REGISTRY_CLAIM_STATE": "managed",
                        failure_flag: "1",
                    },
                )
                self.assertNotEqual(0, denied.returncode)
                self.assert_no_mutations()
                self.assertFalse(
                    [
                        call
                        for call in self._calls()
                        if call["target"] == "registry"
                        and call["path"].endswith("/lifecycle-lease/complete")
                    ]
                )

        for malformed in ("uuid", "expiry", "expired", "action", "status"):
            with self.subTest(malformed_lease=malformed):
                self.calls.unlink(missing_ok=True)
                denied = self._run(
                    "getconsole",
                    self._payload(vmid=114),
                    {
                        "REGISTRY_CLAIM_STATE": "managed",
                        "REGISTRY_MALFORMED_LEASE": malformed,
                    },
                )
                self.assertNotEqual(0, denied.returncode)
                self.assert_no_mutations()
                self.assertFalse(
                    [
                        call
                        for call in self._calls()
                        if call["target"] == "registry"
                        and call["path"].endswith("/lifecycle-lease/complete")
                    ]
                )

        self.calls.unlink(missing_ok=True)
        rerouted = self._run(
            "getconsole",
            self._payload(vmid=999),
            {"REGISTRY_CLAIM_STATE": "managed"},
        )
        self.assertNotEqual(0, rerouted.returncode)
        self.assertFalse(
            [call for call in self._calls() if call["target"] == "registry"]
        )
        self.assert_no_mutations()

        self.calls.unlink(missing_ok=True)
        wrong_node = self._payload(vmid=114)
        wrong_node["externaldetails"]["host"]["node"] = "p2-hv08"
        rejected = self._run(
            "getconsole",
            wrong_node,
            {
                "REGISTRY_CLAIM_STATE": "managed",
                "REGISTRY_EXPECT_NODE": "p2-hv07",
            },
        )
        self.assertNotEqual(0, rejected.returncode)
        self.assert_no_mutations()

    def test_managed_mutation_is_bracketed_by_exact_operation_lease(self):
        result = self._run(
            "stop",
            self._payload(vmid=114),
            {"REGISTRY_CLAIM_STATE": "managed"},
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        calls = self._calls()
        acquire_index = next(
            index
            for index, call in enumerate(calls)
            if call["target"] == "registry"
            and call["path"].endswith("/lifecycle-lease")
        )
        mutation_index = next(
            index
            for index, call in enumerate(calls)
            if call["target"] == "proxmox"
            and call["path"] == "/nodes/p2-hv07/qemu/114/status/stop"
        )
        completion_index = next(
            index
            for index, call in enumerate(calls)
            if call["target"] == "registry"
            and call["path"].endswith("/lifecycle-lease/complete")
        )
        self.assertLess(acquire_index, mutation_index)
        self.assertLess(mutation_index, completion_index)
        self.assertEqual("stop", calls[acquire_index]["body"]["action"])
        self.assertEqual(
            "22222222-2222-4222-8222-222222222222",
            calls[completion_index]["body"]["lease_id"],
        )

    def test_operation_lease_conflict_prevents_proxmox_mutation(self):
        result = self._run(
            "reboot",
            self._payload(vmid=114),
            {
                "REGISTRY_CLAIM_STATE": "managed",
                "REGISTRY_LEASE_CONFLICT": "1",
            },
        )
        self.assertNotEqual(0, result.returncode)
        self.assert_no_mutations()

    def test_failed_proxmox_task_leaves_operation_lease_uncompleted(self):
        result = self._run(
            "stop",
            self._payload(vmid=114),
            {
                "REGISTRY_CLAIM_STATE": "managed",
                "TASK_EXIT_STATUS": "ERROR",
            },
        )
        self.assertNotEqual(0, result.returncode)
        self.assertTrue(
            [
                call
                for call in self._calls()
                if call["target"] == "proxmox"
                and call["path"] == "/nodes/p2-hv07/qemu/114/status/stop"
            ]
        )
        self.assertFalse(
            [
                call
                for call in self._calls()
                if call["target"] == "registry"
                and call["path"].endswith("/lifecycle-lease/complete")
            ]
        )

    def test_restore_state_read_failure_retains_lease_and_reports_error(self):
        (self.fixtures / "status.json").unlink()
        payload = self._payload(vmid=114)
        payload["parameters"] = {"snap_name": "baseline"}
        result = self._run(
            "RestoreSnapshot",
            payload,
            {"REGISTRY_CLAIM_STATE": "managed"},
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Could not verify VM state", result.stdout)
        self.assertTrue(
            [
                call
                for call in self._calls()
                if call["target"] == "proxmox"
                and call["path"]
                == "/nodes/p2-hv07/qemu/114/snapshot/baseline/rollback"
            ]
        )
        self.assertTrue(
            [
                call
                for call in self._calls()
                if call["target"] == "registry"
                and call["path"].endswith("/lifecycle-lease")
            ]
        )
        self.assertFalse(
            [
                call
                for call in self._calls()
                if call["target"] == "registry"
                and call["path"].endswith("/lifecycle-lease/complete")
            ]
        )

    def test_cleared_adoption_flag_cannot_select_ordinary_mutation_paths(self):
        payload = self._payload(vmid=114)
        payload["externaldetails"]["virtualmachine"]["adopt_existing"] = "false"

        stop_result = self._run("stop", payload)
        self.assertNotEqual(0, stop_result.returncode)
        self.assert_no_mutations()

        self.calls.unlink(missing_ok=True)
        delete_result = self._run("delete", payload)
        self.assertEqual(0, delete_result.returncode, delete_result.stdout + delete_result.stderr)
        self.assert_no_mutations()
        self.assertTrue(
            [
                call
                for call in self._calls()
                if call["target"] == "registry" and "/retire" in call["path"]
            ]
        )

    def test_batch_status_uses_bound_cloudstack_name_not_proxmox_name(self):
        result = self._run("statuses", self._payload(vmid=114))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        power_state = json.loads(result.stdout)["power_state"]
        self.assertEqual({"i-2-114-VM": "poweron"}, power_state)
        self.assertNotIn("LTS-NP2-GLR01", power_state)

    def test_mixed_batch_translates_adopted_and_preserves_ordinary_names(self):
        self._write_json(
            "node-vms.json",
            {
                "data": [
                    {
                        "vmid": 114,
                        "name": "LTS-NP2-GLR01",
                        "template": 0,
                        "status": "running",
                    },
                    {
                        "vmid": 115,
                        "name": "ORDINARY-PVE-NAME",
                        "template": 0,
                        "status": "stopped",
                    },
                ]
            },
        )
        result = self._run("statuses", self._payload(vmid=114))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(
            {
                "i-2-114-VM": "poweron",
                "ORDINARY-PVE-NAME": "poweroff",
            },
            json.loads(result.stdout)["power_state"],
        )
        self.assert_no_mutations()

    def test_non_adoption_batch_status_has_no_registry_dependency(self):
        payload = self._payload(vmid=114)
        payload["externaldetails"]["virtualmachine"] = {}
        payload["externaldetails"]["host"].pop(
            "adoption_status_registry_required"
        )
        result = self._run(
            "statuses",
            payload,
            {
                "ADOPTION_REGISTRY_URL": "",
                "ADOPTION_REGISTRY_HEADER_FILE": "",
            },
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        power_state = json.loads(result.stdout)["power_state"]
        self.assertEqual({"LTS-NP2-GLR01": "poweron"}, power_state)
        self.assertEqual(
            {"proxmox"},
            {call["target"] for call in self._calls()},
        )

    def test_adoption_status_registry_outage_is_visible_and_fail_closed(self):
        result = self._run(
            "statuses",
            self._payload(vmid=114),
            {
                "ADOPTION_REGISTRY_URL": "",
                "ADOPTION_REGISTRY_HEADER_FILE": "",
            },
        )
        self.assertNotEqual(0, result.returncode)
        output = json.loads(result.stdout)
        self.assertEqual("error", output["status"])
        self.assertIn("HTTPS", output["error"])
        self.assert_no_mutations()

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
        output = json.loads(result.stdout)
        self.assertEqual("error", output["status"])
        self.assertIn("registry", output["error"].lower())
        self.assert_no_mutations()

    def test_bound_adoption_snapshot_mutations_are_rejected(self):
        for action in ("CreateSnapshot", "RestoreSnapshot", "DeleteSnapshot"):
            with self.subTest(action=action):
                self.calls.unlink(missing_ok=True)
                payload = self._payload(vmid=114)
                payload["parameters"] = {"snap_name": "blocked-snapshot"}
                result = self._run(action, payload)
                self.assertNotEqual(0, result.returncode)
                self.assert_no_mutations()

    def test_managed_adoption_snapshot_mutations_use_proxmox_snapshot_api(self):
        expected = {
            "CreateSnapshot": ("POST", "/nodes/p2-hv07/qemu/114/snapshot"),
            "RestoreSnapshot": (
                "POST",
                "/nodes/p2-hv07/qemu/114/snapshot/managed-snapshot/rollback",
            ),
            "DeleteSnapshot": (
                "DELETE",
                "/nodes/p2-hv07/qemu/114/snapshot/managed-snapshot",
            ),
        }
        for action, expected_mutation in expected.items():
            with self.subTest(action=action):
                self.calls.unlink(missing_ok=True)
                payload = self._payload(vmid=114)
                payload["parameters"] = {"snap_name": "managed-snapshot"}
                result = self._run(
                    action,
                    payload,
                    {"REGISTRY_CLAIM_STATE": "managed"},
                )
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                mutations = [
                    (call["method"], call["path"])
                    for call in self._calls()
                    if call["target"] == "proxmox"
                    and call["method"] in {"POST", "PUT", "DELETE"}
                ]
                self.assertEqual([expected_mutation], mutations)

    def test_managed_lifecycle_fails_closed_when_registry_is_unavailable(self):
        result = self._run(
            "stop",
            self._payload(vmid=114),
            {"REGISTRY_CLAIM_STATE": "managed", "REGISTRY_FAIL": "1"},
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("registry", json.loads(result.stdout)["error"].lower())
        self.assert_no_mutations()

    def test_managed_lifecycle_rejects_user_editable_vmid_rerouting(self):
        payload = self._payload(vmid=114)
        payload["cloudstack.vm.details"]["details"]["proxmox_vmid"] = "999"
        result = self._run(
            "stop",
            payload,
            {
                "REGISTRY_CLAIM_STATE": "managed",
                "REGISTRY_EXPECT_VMID": "114",
            },
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("vmid", json.loads(result.stdout)["error"].lower())
        self.assert_no_mutations()
        self.assertFalse(
            [call for call in self._calls() if call["target"] == "registry"]
        )

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
