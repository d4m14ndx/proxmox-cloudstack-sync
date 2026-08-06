#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  printf 'Usage: %s CLUSTER:VMID MANIFEST_SHA256 [--nic-ip netN=IPv4 ...]\n' "${0##*/}" >&2
  printf 'Example: %s p3-cluster03:110 %s --nic-ip net0=192.0.2.10\n' \
    "${0##*/}" "$(printf 'a%.0s' {1..64})" >&2
}

if [[ $# -lt 2 ]]; then
  usage
  exit 2
fi

proxmox_id=$1
manifest_sha256=$2
shift 2
network_ip_args=()
network_ip_devices=()
while (( $# )); do
  if [[ $1 != --nic-ip || $# -lt 2 ]]; then
    printf 'adoption_stop=unknown_or_incomplete_option\n' >&2
    usage
    exit 2
  fi
  network_ip=$2
  if [[ ! $network_ip =~ ^net(0|[1-9][0-9]*)=((25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.){3}(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])$ ]]; then
    printf 'adoption_stop=network_ip_must_be_canonical_net_device_equals_ipv4\n' >&2
    exit 2
  fi
  device=${network_ip%%=*}
  for seen_device in "${network_ip_devices[@]-}"; do
    if [[ $seen_device == "$device" ]]; then
      printf 'adoption_stop=network_ip_device_is_duplicate\n' >&2
      exit 2
    fi
  done
  network_ip_devices+=("$device")
  network_ip_args+=(--nic-ip "$network_ip")
  shift 2
done

if [[ ! $proxmox_id =~ ^[^[:space:]:]+:[1-9][0-9]*$ ]]; then
  printf 'adoption_stop=proxmox_id_must_be_canonical_cluster_colon_vmid\n' >&2
  exit 2
fi
if [[ ! $manifest_sha256 =~ ^[0-9a-f]{64}$ ]]; then
  printf 'adoption_stop=manifest_sha256_must_be_lowercase_hex\n' >&2
  exit 2
fi

repo_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
cd "$repo_dir"
git fetch --quiet origin main
[[ $(git branch --show-current) == main ]] || {
  printf 'adoption_stop=operator_repo_branch_not_main\n' >&2
  exit 1
}
head_revision=$(git rev-parse HEAD)
origin_revision=$(git rev-parse origin/main)
[[ $head_revision == "$origin_revision" ]] || {
  printf 'adoption_stop=operator_repo_not_exact_origin_main\n' >&2
  exit 1
}
[[ -z $(git status --porcelain) ]] || {
  printf 'adoption_stop=operator_repo_not_clean\n' >&2
  exit 1
}

runtime_files=()
while IFS= read -r file; do
  runtime_files+=("$file")
done < <(git ls-files 'backend/*.py')
[[ ${#runtime_files[@]} -gt 0 ]] || {
  printf 'adoption_stop=runtime_file_manifest_empty\n' >&2
  exit 1
}

reviewed_files=(adopt-one.sh Dockerfile docker-compose.yml "${runtime_files[@]}")
for file in "${reviewed_files[@]}"; do
  git ls-files --error-unmatch "$file" >/dev/null 2>&1 || {
    printf 'adoption_stop=runtime_file_not_tracked file=%s\n' "$file" >&2
    exit 1
  }
done

for file in "${runtime_files[@]}"; do
  host_hash=$(sha256sum "$file")
  host_hash=${host_hash%% *}
  container_hash=$(docker compose exec -T sync sha256sum "/app/$file")
  container_hash=${container_hash%% *}
  [[ $container_hash == "$host_hash" ]] || {
    printf 'adoption_stop=container_source_attestation_failed file=%s\n' "$file" >&2
    exit 1
  }
done

wrapper_hash=$(sha256sum adopt-one.sh)
wrapper_hash=${wrapper_hash%% *}
printf 'deployed_revision=%s\n' "$head_revision"
printf 'operator_wrapper_sha256=%s\n' "$wrapper_hash"
printf 'adoption_source_attestation=PASS\n'

if (( ${#network_ip_args[@]} )); then
  exec docker compose exec -T sync python backend/adopt_one.py \
    --proxmox-id "$proxmox_id" \
    --manifest-sha256 "$manifest_sha256" \
    "${network_ip_args[@]}"
fi
exec docker compose exec -T sync python backend/adopt_one.py \
  --proxmox-id "$proxmox_id" \
  --manifest-sha256 "$manifest_sha256"
