import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from adopt_one import (
    OperatorStop,
    _validate_live_runtime,
    _validate_phase_pair,
    build_parser,
)


class AdoptOneSafetyTests(unittest.TestCase):
    def test_phase_pair_rejects_bound_or_managed_planned_execution(self):
        execution = {
            "id": "execution-id",
            "state": "planned",
            "cloudstack_vm_ref": None,
            "cloudstack_instance_name": None,
            "deploy_job_id": None,
            "start_job_id": None,
        }
        invalid_claims = (
            {
                "state": "bound",
                "cloudstack_vm_ref": "execution-id",
                "cloudstack_instance_name": "i-2-164-VM",
            },
            {
                "state": "managed",
                "cloudstack_vm_ref": "execution-id",
                "cloudstack_instance_name": "i-2-164-VM",
            },
        )
        for claim in invalid_claims:
            with self.subTest(state=claim["state"]), self.assertRaisesRegex(
                OperatorStop, "phase_pair_invalid"
            ):
                _validate_phase_pair(claim, execution)

    def test_phase_pair_accepts_only_correlated_bound_resume(self):
        execution = {
            "id": "execution-id",
            "state": "deploy_submitted",
            "cloudstack_vm_ref": None,
            "cloudstack_instance_name": None,
        }
        _validate_phase_pair(
            {
                "state": "bound",
                "cloudstack_vm_ref": "execution-id",
                "cloudstack_instance_name": "i-2-164-VM",
            },
            execution,
        )
        with self.assertRaisesRegex(OperatorStop, "phase_pair_invalid"):
            _validate_phase_pair(
                {
                    "state": "bound",
                    "cloudstack_vm_ref": "different-vm",
                    "cloudstack_instance_name": "i-2-164-VM",
                },
                execution,
            )

    def test_live_runtime_gate_requires_all_exact_false_booleans(self):
        safe = {
            "runtime_safety": {
                "adoption_executor_enabled": False,
                "auto_reconcile": False,
                "auto_reconcile_nics": False,
            }
        }
        _validate_live_runtime(safe)
        for field in safe["runtime_safety"]:
            unsafe = {"runtime_safety": dict(safe["runtime_safety"])}
            unsafe["runtime_safety"][field] = True
            with self.subTest(field=field), self.assertRaisesRegex(
                OperatorStop, "live_runtime_safety_gate_failed"
            ):
                _validate_live_runtime(unsafe)
        with self.assertRaises(OperatorStop):
            _validate_live_runtime({})

    def test_cli_has_no_arbitrary_base_url_option(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args([
                "--proxmox-id",
                "p3:110",
                "--manifest-sha256",
                "a" * 64,
                "--base-url",
                "https://attacker.example",
            ])


if __name__ == "__main__":
    unittest.main()
