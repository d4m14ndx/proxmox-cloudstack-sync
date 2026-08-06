import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "adopt-one.sh"
RUNTIME_FILES = (
    "backend/adoption.py",
    "backend/adoption_authority.py",
    "backend/adopt_one.py",
    "backend/adoption_executor.py",
    "backend/adoption_registry.py",
    "backend/cloudstack_client.py",
    "backend/cloudstack_db.py",
    "backend/config.py",
    "backend/database.py",
    "backend/main.py",
    "backend/proxmox_client.py",
    "backend/sync_engine.py",
)


class AdoptOneWrapperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        shutil.copy2(WRAPPER, self.root / "adopt-one.sh")
        (self.root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        (self.root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        for relative in RUNTIME_FILES:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"reviewed {relative}\n", encoding="utf-8")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self._write_executable(
            "git",
            """
            #!/usr/bin/env bash
            set -eu
            case "${1:-} ${2:-}" in
              "fetch --quiet") exit 0 ;;
              "branch --show-current") printf '%s\n' "${FAKE_BRANCH:-main}" ;;
              "rev-parse HEAD") printf '%s\n' "${FAKE_HEAD:-reviewed-sha}" ;;
              "rev-parse origin/main") printf '%s\n' "${FAKE_ORIGIN_MAIN:-reviewed-sha}" ;;
              "status --porcelain") printf '%s' "${FAKE_STATUS:-}" ;;
              "ls-files backend/*.py") printf '%s\n' backend/*.py ;;
              "ls-files --error-unmatch")
                [[ ${3:-} != "${FAKE_UNTRACKED_FILE:-never}" ]]
                ;;
              *) exit 97 ;;
            esac
            """,
        )
        self._write_executable(
            "docker",
            """
            #!/usr/bin/env bash
            set -eu
            for argument in "$@"; do
              case "$argument" in
                /app/*)
                  relative=${argument#/app/}
                  if [[ $relative == "${FAKE_MISMATCH_FILE:-never}" ]]; then
                    printf '%064d  %s\n' 0 "$argument"
                  else
                    sha256sum "$relative"
                  fi
                  exit 0
                  ;;
              esac
            done
            for argument in "$@"; do
              printf 'runner_argument=%s\n' "$argument"
            done
            printf 'runner_invoked=PASS\n'
            """,
        )

    def tearDown(self):
        self.temp.cleanup()

    def _write_executable(self, name, content):
        path = self.bin / name
        path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
        path.chmod(0o755)

    def _run(self, *extra_args, **environment):
        env = dict(os.environ)
        env.update(environment)
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        return subprocess.run(
            [
                "bash",
                str(self.root / "adopt-one.sh"),
                "p3-cluster03:110",
                "a" * 64,
                *extra_args,
            ],
            cwd=self.root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_exact_clean_origin_main_and_matching_container_pass(self):
        result = self._run()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("adoption_source_attestation=PASS", result.stdout)
        self.assertIn("runner_invoked=PASS", result.stdout)

    def test_nic_ip_is_forwarded_as_exact_runner_argument(self):
        result = self._run("--nic-ip", "net0=192.0.2.10")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("runner_argument=--nic-ip", result.stdout)
        self.assertIn("runner_argument=net0=192.0.2.10", result.stdout)

    def test_unknown_duplicate_or_noncanonical_nic_ip_stops_before_runner(self):
        for args in (
            ("net0=192.0.2.10",),
            ("--unknown", "net0=192.0.2.10"),
            ("--nic-ip", "net01=192.0.2.10"),
            ("--nic-ip", "net0=192.0.2.10", "--nic-ip", "net0=192.0.2.11"),
        ):
            with self.subTest(args=args):
                result = self._run(*args)
                self.assertNotEqual(0, result.returncode)
                self.assertNotIn("runner_invoked", result.stdout)

    def test_dirty_or_untracked_checkout_stops(self):
        result = self._run(FAKE_STATUS="?? backend/other.py\n")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("operator_repo_not_clean", result.stderr)
        self.assertNotIn("runner_invoked", result.stdout)

    def test_diverged_origin_main_stops(self):
        result = self._run(FAKE_ORIGIN_MAIN="different-sha")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("operator_repo_not_exact_origin_main", result.stderr)
        self.assertNotIn("runner_invoked", result.stdout)

    def test_untracked_runtime_file_stops(self):
        result = self._run(FAKE_UNTRACKED_FILE="backend/adopt_one.py")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("runtime_file_not_tracked", result.stderr)
        self.assertNotIn("runner_invoked", result.stdout)

    def test_container_source_mismatch_stops(self):
        for runtime_file in RUNTIME_FILES:
            with self.subTest(runtime_file=runtime_file):
                result = self._run(FAKE_MISMATCH_FILE=runtime_file)
                self.assertNotEqual(0, result.returncode)
                self.assertIn("container_source_attestation_failed", result.stderr)
                self.assertNotIn("runner_invoked", result.stdout)


if __name__ == "__main__":
    unittest.main()
