#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  printf 'Usage: %s CLUSTER:VMID MANIFEST_SHA256\n' "${0##*/}" >&2
  printf 'Example: %s p3-cluster03:110 %s\n' "${0##*/}" "$(printf 'a%.0s' {1..64})" >&2
}

if [[ $# -ne 2 ]]; then
  usage
  exit 2
fi

proxmox_id=$1
manifest_sha256=$2

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

exec docker compose exec -T sync python backend/adopt_one.py \
  --proxmox-id "$proxmox_id" \
  --manifest-sha256 "$manifest_sha256"
