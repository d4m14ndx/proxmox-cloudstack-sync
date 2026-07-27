#!/usr/bin/env bash
set -euo pipefail

config_file="${PROXMOX_ADOPTION_REGISTRY_CONFIG:-/etc/cloudstack/management/proxmox-adoption-registry.conf}"
if [[ ! -r "$config_file" ]]; then
    printf '{"status":"error","error":"Adoption registry configuration is unreadable"}\n'
    exit 1
fi

# The file must be root-owned and writable only by root during deployment.
# shellcheck disable=SC1090
source "$config_file"

: "${ADOPTION_REGISTRY_URL:?ADOPTION_REGISTRY_URL is required}"
: "${ADOPTION_REGISTRY_HEADER_FILE:?ADOPTION_REGISTRY_HEADER_FILE is required}"
export ADOPTION_REGISTRY_URL ADOPTION_REGISTRY_HEADER_FILE
if [[ -n "${ADOPTION_REGISTRY_CA_FILE:-}" ]]; then
    export ADOPTION_REGISTRY_CA_FILE
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec "$script_dir/proxmox.sh" "$@"
