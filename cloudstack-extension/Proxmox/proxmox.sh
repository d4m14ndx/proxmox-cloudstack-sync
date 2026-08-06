#!/usr/bin/env bash
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

# Payload fields are assigned dynamically by parse_json. Initializing them makes
# fail-closed empty values explicit and keeps static analysis authoritative.
verify_tls_certificate=""
node=""
adopt_existing=""
adopt_claim_id=""
adopt_claim_generation=""
adopt_manifest_sha256=""
adopt_manifest_json=""
adopt_execution_plan_sha256=""
adopt_ip_overrides_json=""
proxmox_cluster=""
adoption_status_registry_required=""
cloudstack_vm_ref=""
template_type=""
iso_path=""
iso_os_type=""
disk_size_gb=""
is_full_clone=""
template_id=""
vlans=""
mac_addresses=""
network_bridge=""
snap_name=""

parse_json() {
    local json_string="$1"
    echo "$json_string" | jq '.' > /dev/null || { echo '{"status": "error", "error": "Invalid JSON input"}'; exit 1; }

    local -A details
    while IFS="=" read -r key value; do
        details[$key]="$value"
    done < <(echo "$json_string" | jq -r '{
        "extension_url":    (.externaldetails.extension.url // ""),
        "extension_user":   (.externaldetails.extension.user // ""),
        "extension_token":  (.externaldetails.extension.token // ""),
        "extension_secret": (.externaldetails.extension.secret // ""),
        "host_url":         (.externaldetails.host.url // ""),
        "host_user":        (.externaldetails.host.user // ""),
        "host_token":       (.externaldetails.host.token // ""),
        "host_secret":      (.externaldetails.host.secret // ""),
        "node":             (.externaldetails.host.node // ""),
        "proxmox_cluster":  (.externaldetails.virtualmachine.proxmox_cluster // .externaldetails.host.proxmox_cluster // ""),
        "adoption_status_registry_required": (.externaldetails.host.adoption_status_registry_required // "false"),
        "network_bridge":   (.externaldetails.host.network_bridge // ""),
        "verify_tls_certificate": (.externaldetails.host.verify_tls_certificate // "true"),
        "vm_name":          (.externaldetails.virtualmachine.vm_name // ""),
        "template_id":      (.externaldetails.virtualmachine.template_id // ""),
        "template_type":    (.externaldetails.virtualmachine.template_type // ""),
        "iso_path":         (.externaldetails.virtualmachine.iso_path // ""),
        "iso_os_type":      (.externaldetails.virtualmachine.iso_os_type // "l26"),
        "disk_size_gb":     (.externaldetails.virtualmachine.disk_size_gb // "64"),
        "storage":          (.externaldetails.virtualmachine.storage // "local-lvm"),
        "is_full_clone":    (.externaldetails.virtualmachine.is_full_clone // "false"),
        "adopt_existing":   (.externaldetails.virtualmachine.adopt_existing // "false"),
        "adopt_claim_id":   (.externaldetails.virtualmachine.adopt_claim_id // ""),
        "adopt_claim_generation": (.externaldetails.virtualmachine.adopt_claim_generation // ""),
        "adopt_manifest_sha256": (.externaldetails.virtualmachine.adopt_manifest_sha256 // ""),
        "adopt_manifest_json": (.externaldetails.virtualmachine.adopt_manifest_json // ""),
        "adopt_execution_plan_sha256": (.externaldetails.virtualmachine.adopt_execution_plan_sha256 // ""),
        "adopt_ip_overrides_json": (.externaldetails.virtualmachine.adopt_ip_overrides_json // ""),
        "snap_name":        (.parameters.snap_name // ""),
        "snap_description": (.parameters.snap_description // ""),
        "snap_save_memory": (.parameters.snap_save_memory // ""),
        "vmid":             (."cloudstack.vm.details".details.proxmox_vmid // ""),
        "cloudstack_vm_ref": (.virtualmachineid // ."cloudstack.vm.details".uuid // ."cloudstack.vm.details".id // "" | tostring),
        "vm_internal_name": (.virtualmachinename // ."cloudstack.vm.details".name // ""),
        "vmmemory":         (."cloudstack.vm.details".minRam // ""),
        "vmcpus":           (."cloudstack.vm.details".cpus // ""),
        "vlans":            ([."cloudstack.vm.details".nics[]?.broadcastUri // "" | sub("vlan://"; "")] | join(",")),
        "mac_addresses":    ([."cloudstack.vm.details".nics[]?.mac // ""] | join(","))
    } | to_entries | .[] | "\(.key)=\(.value)"')

    for key in "${!details[@]}"; do
        declare -g "$key=${details[$key]}"
    done

    # set url, user, token, secret to host values if present, otherwise use extension values
    url="${host_url:-$extension_url}"
    user="${host_user:-$extension_user}"
    token="${host_token:-$extension_token}"
    secret="${host_secret:-$extension_secret}"

    check_required_fields url user token secret node
}

urlencode() {
    python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$1"
}

check_required_fields() {
    local missing=()
    for varname in "$@"; do
        local value="${!varname}"
        if [[ -z "$value" ]]; then
            missing+=("$varname")
        fi
    done

    if [[ ${#missing[@]} -gt 0 ]]; then
        echo "{\"error\":\"Missing required fields: ${missing[*]}\"}"
        exit 1
    fi
}

validate_name() {
    local entity="$1"
    local name="$2"
    if [[ ! "$name" =~ ^[a-zA-Z0-9-]+$ ]]; then
        echo "{\"error\":\"Invalid $entity name '$name'. Only alphanumeric characters and dashes (-) are allowed.\"}"
        exit 1
    fi
}

call_proxmox_api() {
    local method=$1
    local path=$2
    local data=$3

    curl_opts=(
      -s
      --fail
      -X "$method"
      -H "Authorization: PVEAPIToken=${user}!${token}=${secret}"
    )

    if [[ "$verify_tls_certificate" == "false" ]]; then
      curl_opts+=(-k)
    fi

    if [[ -n "$data" ]]; then
      curl_opts+=(-d "$data")
    fi

    response=$(curl "${curl_opts[@]}" "https://${url}:8006/api2/json${path}")
    local status=$?
    if [[ $status -ne 0 ]]; then
        echo "{\"errors\":{\"curl\":\"API call failed with status $status: $(echo "$response" | jq -Rsa . | jq -r .)\"}}"
        return $status
    fi
    echo "$response"
    return 0
}

call_adoption_registry() {
    local method="$1"
    local path="$2"
    local body="${3:-}"
    local registry_url="${ADOPTION_REGISTRY_URL:-}"
    local header_file="${ADOPTION_REGISTRY_HEADER_FILE:-}"
    local ca_file="${ADOPTION_REGISTRY_CA_FILE:-}"
    local body_file=""
    local response status

    [[ "$registry_url" =~ ^https:// ]] || adoption_error "Adoption registry URL must use HTTPS"
    [[ -r "$header_file" ]] || adoption_error "Adoption registry header file is not readable"
    if [[ -n "$ca_file" && ! -r "$ca_file" ]]; then
        adoption_error "Adoption registry CA file is not readable"
    fi

    registry_opts=(
      -s
      --fail
      -X "$method"
      -H "@$header_file"
      -H "Content-Type: application/json"
    )
    if [[ -n "$ca_file" ]]; then
        registry_opts+=(--cacert "$ca_file")
    fi
    if [[ -n "$body" ]]; then
        body_file=$(mktemp)
        chmod 600 "$body_file"
        printf '%s' "$body" >"$body_file"
        registry_opts+=(--data-binary "@$body_file")
    fi

    response=$(curl "${registry_opts[@]}" "${registry_url%/}${path}")
    status=$?
    [[ -z "$body_file" ]] || rm -f "$body_file"
    if [[ $status -ne 0 ]]; then
        adoption_error "Adoption registry request failed"
    fi
    printf '%s\n' "$response"
}

adoption_claim_body() {
    local require_callback_vmid="${1:-false}"
    check_required_fields adopt_claim_id adopt_claim_generation proxmox_cluster \
        adopt_manifest_sha256 cloudstack_vm_ref vm_internal_name
    [[ "$adopt_claim_generation" =~ ^[1-9][0-9]*$ ]] || \
        adoption_error "Invalid adoption claim generation"
    local expected_vmid
    expected_vmid=$(jq -er '.vmid' <<<"$adopt_manifest_json") || adoption_error "Missing adoption VMID"
    [[ "$expected_vmid" =~ ^[1-9][0-9]*$ ]] || \
        adoption_error "Invalid adoption manifest VMID"
    if [[ "$require_callback_vmid" == "true" ]]; then
        [[ "$vmid" =~ ^[1-9][0-9]*$ ]] || adoption_error "Invalid callback Proxmox VMID"
        [[ "$vmid" == "$expected_vmid" ]] || \
            adoption_error "Callback Proxmox VMID does not match adoption manifest"
    elif [[ -n "$vmid" && "$vmid" != "$expected_vmid" ]]; then
        adoption_error "Callback Proxmox VMID does not match adoption manifest"
    fi
    local callback_binding='{}'
    if [[ -n "$adopt_execution_plan_sha256" || -n "$adopt_ip_overrides_json" ]]; then
        [[ "$adopt_execution_plan_sha256" =~ ^[0-9a-f]{64}$ ]] || \
            adoption_error "Invalid adoption execution plan SHA-256"
        [[ -n "$adopt_ip_overrides_json" ]] || \
            adoption_error "Missing adoption IP overrides"
        callback_binding=$(jq -cn \
            --arg plan_sha256 "$adopt_execution_plan_sha256" \
            --arg ip_overrides_json "$adopt_ip_overrides_json" \
            '{execution_plan_sha256:$plan_sha256,ip_overrides_json:$ip_overrides_json}')
    fi
    jq -cn \
        --argjson generation "$adopt_claim_generation" \
        --arg cluster "$proxmox_cluster" \
        --arg node "$node" \
        --argjson vmid "$expected_vmid" \
        --arg manifest "$adopt_manifest_sha256" \
        --arg vm_ref "$cloudstack_vm_ref" \
        --arg instance_name "$vm_internal_name" \
        --argjson callback_binding "$callback_binding" \
        '{generation:$generation,proxmox_cluster:$cluster,proxmox_node:$node,
          proxmox_vmid:$vmid,manifest_sha256:$manifest,
          cloudstack_vm_ref:$vm_ref,cloudstack_instance_name:$instance_name} + $callback_binding'
}

bind_adoption_claim() {
    local body response
    body=$(adoption_claim_body) || {
        printf '%s\n' "$body"
        return 1
    }
    response=$(call_adoption_registry POST \
        "/api/internal/adoption/claims/${adopt_claim_id}/bind" "$body") || {
        printf '%s\n' "$response"
        return 1
    }
    jq -e '.status == "bound"' <<<"$response" >/dev/null || adoption_error "Adoption claim was not bound"
}

authorize_adoption_cleanup_delete() {
    local body response
    body=$(adoption_claim_body) || return 1
    response=$(call_adoption_registry POST \
        "/api/internal/adoption/claims/${adopt_claim_id}/authorize-cleanup-delete" \
        "$body") || return 1
    jq -e --arg execution_id "$cloudstack_vm_ref" \
        '.status == "cleanup_delete_authorized" and .execution_id == $execution_id' \
        <<<"$response" >/dev/null
}

adoption_claim_state() {
    local body response state
    body=$(adoption_claim_body true) || {
        printf '%s\n' "$body"
        return 1
    }
    response=$(call_adoption_registry POST \
        "/api/internal/adoption/claims/${adopt_claim_id}/lifecycle-state" "$body") || {
        printf '%s\n' "$response"
        return 1
    }
    state=$(jq -er '.state | select(. == "bound" or . == "managed" or . == "operating" or . == "retiring")' \
        <<<"$response") || adoption_error "Adoption registry returned an invalid lifecycle state"
    printf '%s\n' "$state"
}


require_managed_adoption() {
    local state
    state=$(adoption_claim_state) || {
        printf '%s\n' "$state"
        return 1
    }
    [[ "$state" == "managed" ]] || \
        adoption_error "Adopted instance lifecycle is not managed"
}


begin_managed_adoption_operation() {
    local operation="$1" body response lease_id expires_at expires_epoch now_epoch
    body=$(adoption_claim_body true) || {
        printf '%s\n' "$body"
        return 1
    }
    body=$(jq -c --arg action "$operation" '. + {action:$action}' <<<"$body") || \
        adoption_error "Could not construct managed operation lease request"
    response=$(call_adoption_registry POST \
        "/api/internal/adoption/claims/${adopt_claim_id}/lifecycle-lease" "$body") || {
        printf '%s\n' "$response"
        return 1
    }
    lease_id=$(jq -er --arg action "$operation" \
        'select(.status == "operating" and .action == $action)
         | .lease_id
         | select(type == "string")
         | select(test("^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"))' \
        <<<"$response") || \
        adoption_error "Adoption registry returned an invalid operation lease"
    expires_at=$(jq -er \
        '.expires_at
         | select(type == "string")
         | select(test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})$"))' \
        <<<"$response") || \
        adoption_error "Adoption registry returned an invalid operation lease expiry"
    expires_epoch=$(date -u -d "$expires_at" +%s 2>/dev/null) || \
        adoption_error "Adoption registry returned an invalid operation lease expiry"
    now_epoch=$(date -u +%s) || adoption_error "Could not validate operation lease expiry"
    (( expires_epoch > now_epoch )) || \
        adoption_error "Adoption registry returned an expired operation lease"
    printf '%s\n' "$lease_id"
}


complete_managed_adoption_operation() {
    local operation="$1" lease_id="$2" body response
    body=$(adoption_claim_body true) || {
        printf '%s\n' "$body"
        return 1
    }
    body=$(jq -c --arg action "$operation" --arg lease_id "$lease_id" \
        '. + {action:$action,lease_id:$lease_id}' <<<"$body") || \
        adoption_error "Could not construct managed operation completion request"
    response=$(call_adoption_registry POST \
        "/api/internal/adoption/claims/${adopt_claim_id}/lifecycle-lease/complete" "$body") || {
        printf '%s\n' "$response"
        return 1
    }
    jq -e --arg lease_id "$lease_id" \
        '.status == "ok" and .state == "managed" and .lease_id == $lease_id' \
        <<<"$response" >/dev/null || \
        adoption_error "Adoption registry did not complete the operation lease"
}


retire_adoption_claim() {
    local body response
    body=$(adoption_claim_body true) || {
        printf '%s\n' "$body"
        return 1
    }
    response=$(call_adoption_registry POST \
        "/api/internal/adoption/claims/${adopt_claim_id}/retire" "$body") || {
        printf '%s\n' "$response"
        return 1
    }
    jq -e '.status == "retiring"' <<<"$response" >/dev/null || adoption_error "Adoption claim was not tombstoned"
}

wait_for_proxmox_task() {
    local upid="$1"
    local timeout="${2:-$wait_time}"
    local interval="${3:-1}"

    local start_time
    start_time=$(date +%s)

    while true; do
        local now
        now=$(date +%s)
        if (( now - start_time > timeout )); then
            echo '{"status": "error", "error":"Timeout while waiting for async task"}'
            exit 1
        fi

        local status_response
        status_response=$(call_proxmox_api GET "/nodes/${node}/tasks/$(urlencode "$upid")/status")

        if [[ -z "$status_response" || "$status_response" == *'"errors":'* ]]; then
            local msg
            msg=$(echo "$status_response" | jq -r '.message // "Unknown error"')
            echo "{\"status\": \"error\", \"error\": \"$msg\"}"
            exit 1
        fi

        local task_status
        task_status=$(echo "$status_response" | jq -r '.data.status')

        if [[ "$task_status" == "stopped" ]]; then
            local exit_status
            exit_status=$(echo "$status_response" | jq -r '.data.exitstatus')
            if [[ "$exit_status" != "OK" ]]; then
                echo "{\"error\":\"Task failed with exit status: $exit_status\"}"
                exit 1
            fi
            return 0
        fi

        sleep "$interval"
    done
}

execute_and_wait() {
    local method="$1"
    local path="$2"
    local data="$3"
    local response upid msg

    response=$(call_proxmox_api "$method" "$path" "$data")
    upid=$(echo "$response" | jq -r '.data // ""')

    if [[ -z "$upid" ]]; then
        msg=$(echo "$response" | jq -r '.message // "Unknown error"')
        echo "{\"error\":\"Failed to execute API or retrieve UPID. Message: $msg\"}"
        exit 1
    fi

    wait_for_proxmox_task "$upid"
}

vm_not_present() {
    response=$(call_proxmox_api GET "/cluster/nextid?vmid=$vmid")
    vmid_result=$(echo "$response" | jq -r '.data // empty')
    if [[ "$vmid_result" == "$vmid" ]]; then
        return 0
    else
        return 1
    fi
}

adoption_error() {
    local message="$1"
    jq -n --arg error "$message" '{status:"error", error:$error}'
    exit 1
}

is_adoption() {
    [[ "$adopt_existing" == "true" \
        || -n "$adopt_claim_id" \
        || -n "$adopt_claim_generation" \
        || -n "$adopt_manifest_sha256" \
        || -n "$adopt_manifest_json" \
        || -n "$adopt_execution_plan_sha256" \
        || -n "$adopt_ip_overrides_json" ]]
}

normalize_mac() {
    tr '[:lower:]' '[:upper:]' <<<"$1"
}

validate_adoption_execution_binding() {
    local manifest="$1" expected_devices observed_devices canonical_overrides
    jq -e '
        [.networks[] | select(.ip == null)] as $unresolved
        | ([.networks[] | select(.ip != null and has("ip_override_required"))] | length == 0)
        and ($unresolved | all(
            (.ip_override_required == true)
            and (.device | type == "string" and test("^net(0|[1-9][0-9]*)$"))
        ))
    ' <<<"$manifest" >/dev/null || adoption_error "Invalid adoption manifest IP override contract"

    if [[ -z "$adopt_execution_plan_sha256" && -z "$adopt_ip_overrides_json" ]]; then
        jq -e '[.networks[] | select(.ip == null)] | length == 0' <<<"$manifest" >/dev/null || \
            adoption_error "Unresolved adoption NIC requires execution binding"
        return 0
    fi
    [[ "$adopt_execution_plan_sha256" =~ ^[0-9a-f]{64}$ ]] || \
        adoption_error "Invalid adoption execution plan SHA-256"
    [[ -n "$adopt_ip_overrides_json" ]] || adoption_error "Missing adoption IP overrides"
    canonical_overrides=$(jq -ceS '
        if type != "array" then error("not an array")
        elif all(.[];
            type == "object"
            and keys == ["device_id", "ip"]
            and (.device_id | type == "number" and floor == . and . >= 0)
            and (.ip | type == "string" and test("^(?:(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\\.){3}(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])$"))
        ) then sort_by(.device_id)
        else error("invalid override") end
    ' <<<"$adopt_ip_overrides_json" 2>/dev/null) || adoption_error "Invalid adoption IP override JSON"
    [[ "$canonical_overrides" == "$adopt_ip_overrides_json" ]] || \
        adoption_error "Adoption IP override JSON is not canonical"
    expected_devices=$(jq -ce '[.networks[] | select(.ip == null) | (.device | ltrimstr("net") | tonumber)] | sort' <<<"$manifest") || \
        adoption_error "Invalid unresolved adoption NIC device"
    observed_devices=$(jq -ce '[.[].device_id] | sort' <<<"$canonical_overrides") || \
        adoption_error "Invalid adoption IP override device"
    [[ "$(jq -c 'unique' <<<"$observed_devices")" == "$observed_devices" ]] || \
        adoption_error "Duplicate adoption IP override device"
    [[ "$observed_devices" == "$expected_devices" ]] || \
        adoption_error "Adoption IP overrides do not exactly match unresolved NICs"
}

validate_adoption_nics() {
    local manifest="$1" config_response="$2" expected_node="$3" expected_vmid="$4"
    local expected_count actual_count guest_response expected_nic device config_value
    local actual_mac actual_bridge actual_tag expected_tag expected_ip manifest_ip
    local network_ip_allocation legacy_external_ip_binding=false
    expected_count=$(jq '.networks | length' <<<"$manifest")
    actual_count=$(jq '[.data | to_entries[] | select(.key | test("^net[0-9]+$"))] | length' <<<"$config_response")
    [[ "$actual_count" == "$expected_count" ]] || adoption_error "Existing NIC count does not match adoption manifest"

    if [[ -z "$adopt_execution_plan_sha256" && -z "$adopt_ip_overrides_json" ]]; then
        legacy_external_ip_binding=true
    fi
    if jq -e --argjson legacy_external "$legacy_external_ip_binding" '
        [.networks[]
         | select(
             .ip != null
             and (((.ip_allocation // "cloudstack") != "external") or ($legacy_external | not))
         )] | length > 0
    ' <<<"$manifest" >/dev/null; then
        guest_response=$(call_proxmox_api GET "/nodes/${expected_node}/qemu/${expected_vmid}/agent/network-get-interfaces") || adoption_error "Could not read existing guest IP identity"
    else
        guest_response='{"data":{"result":[]}}'
    fi

    while IFS= read -r expected_nic; do
        device=$(jq -r '.device' <<<"$expected_nic")
        [[ "$device" =~ ^net[0-9]+$ ]] || adoption_error "Invalid NIC device in adoption manifest"
        config_value=$(jq -er --arg device "$device" '.data[$device]' <<<"$config_response") || adoption_error "Existing NIC device is missing"
        actual_mac=$(sed -E 's/^[^=]+=([^,]+).*$/\1/' <<<"$config_value" | tr '[:lower:]' '[:upper:]')
        actual_bridge=$(tr ',' '\n' <<<"$config_value" | sed -n 's/^bridge=//p')
        actual_tag=$(tr ',' '\n' <<<"$config_value" | sed -n 's/^tag=//p')
        expected_tag=$(jq -r 'if .tag == null then "" else (.tag | tostring) end' <<<"$expected_nic")

        [[ "$actual_mac" == "$(normalize_mac "$(jq -r '.mac' <<<"$expected_nic")")" ]] || adoption_error "Existing NIC MAC does not match adoption manifest"
        [[ "$actual_bridge" == "$(jq -r '.bridge' <<<"$expected_nic")" ]] || adoption_error "Existing NIC bridge does not match adoption manifest"
        [[ "$actual_tag" == "$expected_tag" ]] || adoption_error "Existing NIC VLAN does not match adoption manifest"

        manifest_ip=$(jq -r '.ip // ""' <<<"$expected_nic")
        expected_ip="$manifest_ip"
        if [[ -z "$expected_ip" ]]; then
            expected_ip=$(jq -er --argjson device "${device#net}" \
                '.[] | select(.device_id == $device) | .ip' <<<"$adopt_ip_overrides_json") || \
                adoption_error "Adoption NIC IP override is missing"
        else
            network_ip_allocation=$(jq -r '.ip_allocation // "cloudstack"' <<<"$expected_nic")
            if [[ "$legacy_external_ip_binding" != "true" || "$network_ip_allocation" != "external" ]]; then
                jq -e --arg mac "$actual_mac" --arg ip "$expected_ip" '
                    [.data.result[]
                     | select(((."hardware-address" // "") | ascii_upcase) == $mac)
                     | .["ip-addresses"][]?
                     | select(."ip-address" == $ip)] | length == 1
                ' <<<"$guest_response" >/dev/null || adoption_error "Existing NIC IP does not match the guest agent"
            fi
        fi
    done < <(jq -c '.networks | sort_by(.device)[]' <<<"$manifest")

    local planned_count index planned_mac planned_vlan planned_ip ip_allocation
    planned_count=$(jq '[."cloudstack.vm.details".nics[]?] | length' <<<"$parameters")
    [[ "$planned_count" == "$expected_count" ]] || adoption_error "CloudStack planned NIC count does not match adoption manifest"
    while IFS= read -r expected_nic; do
        device=$(jq -r '.device' <<<"$expected_nic")
        index=${device#net}
        planned_mac=$(jq -r --argjson index "$index" '."cloudstack.vm.details".nics[$index].mac // ""' <<<"$parameters" | tr '[:lower:]' '[:upper:]')
        planned_vlan=$(jq -r --argjson index "$index" '."cloudstack.vm.details".nics[$index].broadcastUri // "" | sub("^vlan://"; "")' <<<"$parameters")
        planned_ip=$(jq -r --argjson index "$index" '."cloudstack.vm.details".nics[$index].ip // ""' <<<"$parameters")
        [[ "$planned_mac" == "$(normalize_mac "$(jq -r '.mac' <<<"$expected_nic")")" ]] || adoption_error "CloudStack planned MAC does not match adoption manifest"
        expected_tag=$(jq -r 'if .tag == null then "" else (.tag | tostring) end' <<<"$expected_nic")
        [[ "$planned_vlan" == "$expected_tag" ]] || adoption_error "CloudStack planned VLAN does not match adoption manifest"
        expected_ip=$(jq -r '.ip // ""' <<<"$expected_nic")
        if [[ -z "$expected_ip" ]]; then
            expected_ip=$(jq -er --argjson device "${device#net}" \
                '.[] | select(.device_id == $device) | .ip' <<<"$adopt_ip_overrides_json") || \
                adoption_error "Adoption NIC IP override is missing"
        fi
        ip_allocation=$(jq -r '.ip_allocation // "cloudstack"' <<<"$expected_nic")
        case "$ip_allocation" in
            cloudstack)
                [[ "$planned_ip" == "$expected_ip" ]] || adoption_error "CloudStack planned IP does not match adoption manifest"
                ;;
            external)
                [[ -z "$planned_ip" || "$planned_ip" == "$expected_ip" ]] || adoption_error "CloudStack planned IP conflicts with external IPAM manifest"
                ;;
            *)
                adoption_error "Invalid NIC IP allocation mode in adoption manifest"
                ;;
        esac
    done < <(jq -c '.networks | sort_by(.device)[]' <<<"$manifest")
}

validate_adoption_disks() {
    local manifest="$1" config_response="$2" expected_node="$3"
    local expected_count actual_count expected_disk device config_value volume storage size status_response
    expected_count=$(jq '.storage | length' <<<"$manifest")
    (( expected_count > 0 )) || adoption_error "Adoption requires at least one non-CD-ROM disk"
    actual_count=$(jq '[.data | to_entries[]
        | select(.key | test("^(scsi|sata|virtio|ide)[0-9]+$|^(efidisk|tpmstate)[0-9]+$"))
        | select((.value | tostring | contains("media=cdrom")) | not)] | length' <<<"$config_response")
    [[ "$actual_count" == "$expected_count" ]] || adoption_error "Existing disk count does not match adoption manifest"

    while IFS= read -r expected_disk; do
        device=$(jq -r '.device' <<<"$expected_disk")
        [[ "$device" =~ ^((scsi|sata|virtio|ide|efidisk|tpmstate)[0-9]+)$ ]] || adoption_error "Invalid disk device in adoption manifest"
        config_value=$(jq -er --arg device "$device" '.data[$device]' <<<"$config_response") || adoption_error "Existing disk device is missing"
        [[ "$config_value" != *"media=cdrom"* ]] || adoption_error "CD-ROM cannot satisfy an adoption disk"
        volume=${config_value%%,*}
        storage=${volume%%:*}
        size=$(tr ',' '\n' <<<"$config_value" | sed -n 's/^size=//p')
        [[ "$volume" == "$(jq -r '.volume' <<<"$expected_disk")" ]] || adoption_error "Existing disk volume does not match adoption manifest"
        [[ "$storage" == "$(jq -r '.storage' <<<"$expected_disk")" ]] || adoption_error "Existing disk storage does not match adoption manifest"
        [[ "$size" == "$(jq -r '.size' <<<"$expected_disk")" ]] || adoption_error "Existing disk size does not match adoption manifest"
        status_response=$(call_proxmox_api GET "/nodes/${expected_node}/storage/${storage}/status") || adoption_error "Could not read Proxmox storage status"
        jq -e '(.data.active // 0) == 1 and (.data.enabled // 0) == 1' <<<"$status_response" >/dev/null || adoption_error "Proxmox storage is not active and enabled"
    done < <(jq -c '.storage | sort_by(.device)[]' <<<"$manifest")
}

validate_adoption_manifest() {
    is_adoption || adoption_error "Adoption validation requested without adopt_existing=true"
    check_required_fields adopt_manifest_sha256 adopt_manifest_json

    if [[ ! "$adopt_manifest_sha256" =~ ^[0-9a-f]{64}$ ]]; then
        adoption_error "Invalid adoption manifest SHA-256"
    fi

    local canonical_manifest actual_hash expected_node expected_vmid expected_name
    local expected_cpus expected_memory expected_status resource_response config_response status_response
    canonical_manifest=$(jq -ceS . <<<"$adopt_manifest_json" 2>/dev/null) || adoption_error "Invalid adoption manifest JSON"
    actual_hash=$(printf '%s' "$canonical_manifest" | sha256sum | cut -d' ' -f1)
    [[ "$actual_hash" == "$adopt_manifest_sha256" ]] || adoption_error "Adoption manifest hash mismatch"
    validate_adoption_execution_binding "$canonical_manifest"

    expected_node=$(jq -er '.placement.node' <<<"$canonical_manifest") || adoption_error "Missing adoption node"
    expected_vmid=$(jq -er '.vmid | select(type == "number" and . > 0 and floor == .)' <<<"$canonical_manifest") || adoption_error "Invalid adoption VMID"
    expected_name=$(jq -er '.name | select(type == "string" and length > 0)' <<<"$canonical_manifest") || adoption_error "Invalid adoption name"
    expected_cpus=$(jq -er '.cpus | select(type == "number" and . > 0 and floor == .)' <<<"$canonical_manifest") || adoption_error "Invalid adoption CPU count"
    expected_memory=$(jq -er '.memory_mib | select(type == "number" and . > 0 and floor == .)' <<<"$canonical_manifest") || adoption_error "Invalid adoption memory"
    expected_status=$(jq -er '.status | ascii_downcase' <<<"$canonical_manifest") || adoption_error "Missing adoption power state"

    [[ "$expected_node" == "$node" ]] || adoption_error "Adoption node does not match scheduled host"
    [[ "$expected_status" == "running" ]] || adoption_error "Only an already-running guest may be adopted"

    if [[ -n "$vmid" && "$vmid" != "$expected_vmid" ]]; then
        adoption_error "CloudStack VMID does not match adoption manifest"
    fi
    if [[ -n "$vmcpus" && "$vmcpus" != "$expected_cpus" ]]; then
        adoption_error "CloudStack CPU count does not match adoption manifest"
    fi
    if [[ -n "$vmmemory" ]]; then
        [[ $((vmmemory / 1024 / 1024)) == "$expected_memory" ]] || adoption_error "CloudStack memory does not match adoption manifest"
    fi

    resource_response=$(call_proxmox_api GET "/cluster/resources?type=vm") || adoption_error "Could not read Proxmox VM inventory"
    jq -e --argjson vmid "$expected_vmid" --arg node "$expected_node" '
        [.data[] | select(.vmid == $vmid and .type == "qemu" and .node == $node and (.template // 0) != 1)] | length == 1
    ' <<<"$resource_response" >/dev/null || adoption_error "Existing QEMU VM identity or placement is not unique"

    config_response=$(call_proxmox_api GET "/nodes/${expected_node}/qemu/${expected_vmid}/config") || adoption_error "Could not read existing QEMU configuration"
    jq -e --arg name "$expected_name" --argjson cpus "$expected_cpus" --argjson memory "$expected_memory" '
        .data as $c
        | (($c.name // "") == $name)
        and (($c.template // 0) != 1)
        and (((($c.sockets // 1) | tonumber) * (($c.cores // 1) | tonumber)) == $cpus)
        and ((($c.memory // 0) | tonumber) == $memory)
    ' <<<"$config_response" >/dev/null || adoption_error "Existing name, CPU, memory or template state does not match"

    status_response=$(call_proxmox_api GET "/nodes/${expected_node}/qemu/${expected_vmid}/status/current") || adoption_error "Could not read existing QEMU power state"
    [[ "$(jq -r '.data.status // ""' <<<"$status_response")" == "running" ]] || adoption_error "Existing QEMU VM is not running"

    validate_adoption_nics "$canonical_manifest" "$config_response" "$expected_node" "$expected_vmid"
    validate_adoption_disks "$canonical_manifest" "$config_response" "$expected_node"
}

prepare() {
    if is_adoption; then
        validate_adoption_manifest
        bind_adoption_claim || return 1
        vmid=$(jq -r '.vmid | tostring' <<<"$adopt_manifest_json")
        jq -n --arg vmid "$vmid" '{details:{proxmox_vmid:$vmid}}'
        return 0
    fi
    response=$(call_proxmox_api GET "/cluster/nextid")
    vmid=$(echo "$response" | jq -r '.data // ""')

    echo "{\"details\":{\"proxmox_vmid\": \"$vmid\"}}"
}

create() {
    if is_adoption; then
        validate_adoption_manifest
        bind_adoption_claim || return 1
        echo '{"status":"success","message":"Existing instance validated and adopted without Proxmox mutation"}'
        return 0
    fi
    if [[ -z "$vm_name" ]]; then
        if [[ -z "$vm_internal_name" ]]; then
            echo '{"error":"Missing required fields: vm_internal_name"}'
            exit 1
        fi
        vm_name="$vm_internal_name"
    fi
    validate_name "VM" "$vm_name"
    check_required_fields vmid network_bridge vmcpus vmmemory

    if [[ "${template_type^^}" == "ISO" ]]; then
        check_required_fields iso_path
        local data="vmid=$vmid"
        data+="&name=$vm_name"
        data+="&ide2=$(urlencode "$iso_path,media=cdrom")"
        data+="&ostype=$iso_os_type"
        data+="&scsihw=virtio-scsi-single"
        data+="&scsi0=$(urlencode "$storage:$disk_size_gb,iothread=on")"
        data+="&sockets=1"
        data+="&cores=$vmcpus"
        data+="&numa=0"
        data+="&cpu=x86-64-v2-AES"
        data+="&memory=$((vmmemory / 1024 / 1024))"

        execute_and_wait POST "/nodes/${node}/qemu/" "$data"
        cleanup_vm=1

    else
        check_required_fields template_id
        local data="newid=$vmid"
        data+="&name=$vm_name"
        clone_flag=$(( is_full_clone == "true" ))
        data+="&storage=$storage&full=$clone_flag"
        execute_and_wait POST "/nodes/${node}/qemu/${template_id}/clone" "$data"
        cleanup_vm=1

        data="cores=$vmcpus"
        data+="&memory=$((vmmemory / 1024 / 1024))"
        execute_and_wait POST "/nodes/${node}/qemu/${vmid}/config" "$data"
    fi

    IFS=',' read -ra vlan_array <<< "$vlans"
    IFS=',' read -ra mac_array <<< "$mac_addresses"
    for i in "${!vlan_array[@]}"; do
        network="net${i}=$(urlencode "virtio=${mac_array[i]},bridge=${network_bridge},tag=${vlan_array[i]},firewall=0")"
        call_proxmox_api PUT "/nodes/${node}/qemu/${vmid}/config/" "$network" > /dev/null
    done

    execute_and_wait POST "/nodes/${node}/qemu/${vmid}/status/start"

    cleanup_vm=0
    echo '{"status": "success", "message": "Instance created"}'
}

start() {
    local operation_lease_id=""
    if is_adoption; then
        local claim_state
        claim_state=$(adoption_claim_state) || {
            printf '%s\n' "$claim_state"
            return 1
        }
        if [[ "$claim_state" == "bound" ]]; then
            validate_adoption_manifest
            echo '{"status":"success","message":"Adoption start acknowledged without Proxmox mutation"}'
            return 0
        fi
        [[ "$claim_state" == "managed" ]] || \
            adoption_error "Adopted instance lifecycle is not managed"
        operation_lease_id=$(begin_managed_adoption_operation start) || {
            printf '%s\n' "$operation_lease_id"
            return 1
        }
    fi
    execute_and_wait POST "/nodes/${node}/qemu/${vmid}/status/start"
    if [[ -n "$operation_lease_id" ]]; then
        complete_managed_adoption_operation start "$operation_lease_id" || return 1
    fi
    echo '{"status": "success", "message": "Instance started"}'
}

delete() {
    if is_adoption; then
        if authorize_adoption_cleanup_delete; then
            echo '{"status":"success","message":"Explicit rollback authorized; CloudStack metadata deleted and Proxmox instance retained"}'
            return 0
        fi
        retire_adoption_claim || return 1
        echo '{"status":"success","message":"CloudStack metadata deletion accepted; adopted Proxmox instance retained and claim tombstoned pending verified CloudStack absence"}'
        return 0
    fi
    if vm_not_present; then
        echo '{"status": "success", "message": "Instance deleted"}'
        return 0
    fi
    execute_and_wait DELETE "/nodes/${node}/qemu/${vmid}"
    echo '{"status": "success", "message": "Instance deleted"}'
}

stop() {
    local operation_lease_id=""
    if is_adoption; then
        require_managed_adoption || return 1
    fi
    if vm_not_present; then
        echo '{"status": "success", "message": "Instance stopped"}'
        return 0
    fi
    if is_adoption; then
        operation_lease_id=$(begin_managed_adoption_operation stop) || {
            printf '%s\n' "$operation_lease_id"
            return 1
        }
    fi
    execute_and_wait POST "/nodes/${node}/qemu/${vmid}/status/stop"
    if [[ -n "$operation_lease_id" ]]; then
        complete_managed_adoption_operation stop "$operation_lease_id" || return 1
    fi
    echo '{"status": "success", "message": "Instance stopped"}'
}

reboot() {
    local operation_lease_id=""
    if is_adoption; then
        require_managed_adoption || return 1
        operation_lease_id=$(begin_managed_adoption_operation reboot) || {
            printf '%s\n' "$operation_lease_id"
            return 1
        }
    fi
    execute_and_wait POST "/nodes/${node}/qemu/${vmid}/status/reboot"
    if [[ -n "$operation_lease_id" ]]; then
        complete_managed_adoption_operation reboot "$operation_lease_id" || return 1
    fi
    echo '{"status": "success", "message": "Instance rebooted"}'
}

status() {
    local status_response vm_status powerstate
    status_response=$(call_proxmox_api GET "/nodes/${node}/qemu/${vmid}/status/current")
    vm_status=$(echo "$status_response" | jq -r '.data.status')
    case "$vm_status" in
        running)  powerstate="poweron"  ;;
        stopped)  powerstate="poweroff" ;;
        *)        powerstate="unknown"  ;;
    esac

    echo "{\"status\": \"success\", \"power_state\": \"$powerstate\"}"
}

get_node_host() {
    check_required_fields node
    local net_json host

    if ! net_json="$(call_proxmox_api GET "/nodes/${node}/network")"; then
        echo ""
        return 1
    fi

    # Prefer a static non-bridge IP
    host="$(echo "$net_json" | jq -r '
        .data
        | map(select(
            (.type // "") != "bridge" and
            (.type // "") != "bond" and
            (.method // "") == "static" and
            ((.address // .cidr // "") != "")
        ))
        | map(.address // (.cidr | split("/")[0]))
        | .[0] // empty
    ' 2>/dev/null)"

    # Fallback: first interface with a CIDR
    if [[ -z "$host" ]]; then
        host="$(echo "$net_json" | jq -r '
            .data
            | map(select((.cidr // "") != ""))
            | map(.cidr | split("/")[0])
            | .[0] // empty
        ' 2>/dev/null)"
    fi

    echo "$host"
}

get_console() {
    check_required_fields node vmid

    local api_resp port ticket operation_lease_id=""
    if is_adoption; then
        require_managed_adoption >/dev/null || return 1
        operation_lease_id=$(begin_managed_adoption_operation console) || {
            printf '%s\n' "$operation_lease_id"
            return 1
        }
    fi

    if ! api_resp="$(call_proxmox_api POST "/nodes/${node}/qemu/${vmid}/vncproxy")"; then
       echo "$api_resp" | jq -c '{status:"error", error:(.errors.curl // (.errors|tostring))}'
       exit 1
    fi

    port="$(echo "$api_resp"   | jq -re '.data.port // empty' 2>/dev/null || true)"
    ticket="$(echo "$api_resp" | jq -re '.data.ticket // empty' 2>/dev/null || true)"

    if [[ -z "$port" || -z "$ticket" ]]; then
       echo '{"status":"error","error":"Proxmox response missing port/ticket"}'
       exit 1
    fi

    # Derive host from node’s network info
    local host
    host="$(get_node_host)"
    if [[ -z "$host" ]]; then
       jq -n --arg msg "Could not determine host IP for node $node" \
           '{status:"error", error:$msg}'
       exit 1
    fi

    if is_adoption; then
        complete_managed_adoption_operation "console" "$operation_lease_id"
    fi

    jq -n \
       --arg host "$host" \
       --arg port "$port" \
       --arg password "$ticket" \
       --argjson passwordonetimeuseonly true \
       '{
           status: "success",
           message: "Console retrieved",
           console: {
               host: $host,
               port: $port,
               password: $password,
               passwordonetimeuseonly: $passwordonetimeuseonly,
               protocol: "vnc"
           }
       }'
}

statuses() {
    local response registry_response status_bindings
    response=$(call_proxmox_api GET "/nodes/${node}/qemu")

    if [[ -z "$response" ]]; then
        echo '{"status":"error","message":"empty response from Proxmox API"}'
        return 1
    fi

    if ! echo "$response" | jq empty >/dev/null 2>&1; then
        echo '{"status":"error","message":"invalid JSON response from Proxmox API"}'
        return 1
    fi

    case "${adoption_status_registry_required,,}" in
      false|"")
        echo "$response" | jq -c '
          def map_state(s):
            if   s=="running" then "poweron"
            elif s=="stopped" then "poweroff"
            else "unknown" end;
          {
            status: "success",
            power_state: (
              [.data[] | select(.template != 1) | {key: (.name // (.vmid | tostring)), value: map_state(.status)}]
              | from_entries
            )
          }
        '
        return $?
        ;;
      true)
        ;;
      *)
        adoption_error "Invalid adoption_status_registry_required host detail"
        ;;
    esac

    check_required_fields proxmox_cluster
    registry_response=$(call_adoption_registry GET "/api/internal/adoption/status-map?proxmox_cluster=$(urlencode "$proxmox_cluster")") || {
        printf '%s\n' "$registry_response"
        return 1
    }
    status_bindings=$(jq -ce '.bindings' <<<"$registry_response") || adoption_error "Invalid adoption status bindings"

    echo "$response" | jq -c --argjson status_bindings "$status_bindings" '
      def map_state(s):
        if   s=="running" then "poweron"
        elif s=="stopped" then "poweroff"
        else "unknown" end;
      {
        status: "success",
        power_state: (
          [ .data[]
            | select(.template != 1)
            | (.vmid | tostring) as $vmid
            | ($status_bindings[$vmid] // null) as $binding
            | if ($binding != null and .name != $binding.expected_proxmox_name)
              then error("bound Proxmox name does not match immutable claim")
              else . end
            | {
                key: (if $binding == null then (.name // $vmid) else $binding.cloudstack_instance_name end),
                value: map_state(.status)
              }
          ] | from_entries
        )
      }
    '
}

list_snapshots() {
    snapshot_response=$(call_proxmox_api GET "/nodes/${node}/qemu/${vmid}/snapshot")
    echo "$snapshot_response" | jq '
        def to_date:
            if . == "-" then "-"
            elif . == null then "-"
            else (. | tonumber | strftime("%Y-%m-%d %H:%M:%S"))
            end;

        {
            status: "success",
            printmessage: "true",
            message: [.data[] | {
                name: .name,
                snaptime: ((.snaptime // "-") | to_date),
                description: .description,
                parent: (.parent // "-"),
                vmstate: (.vmstate // "-")
            }]
        }
    '
}

create_snapshot() {
    local operation_lease_id=""
    check_required_fields snap_name
    validate_name "Snapshot" "$snap_name"

    if is_adoption; then
        require_managed_adoption || return 1
        operation_lease_id=$(begin_managed_adoption_operation create_snapshot) || {
            printf '%s\n' "$operation_lease_id"
            return 1
        }
    fi

    local data vmstate
    data="snapname=$snap_name"
    if [[ -n "$snap_description" ]]; then
        data+="&description=$snap_description"
    fi
    if [[ -n "$snap_save_memory" && "$snap_save_memory" == "true" ]]; then
        vmstate="1"
    else
        vmstate="0"
    fi
    data+="&vmstate=$vmstate"

    execute_and_wait POST "/nodes/${node}/qemu/${vmid}/snapshot" "$data"
    if [[ -n "$operation_lease_id" ]]; then
        complete_managed_adoption_operation create_snapshot "$operation_lease_id" || return 1
    fi
    echo '{"status": "success", "message": "Instance Snapshot created"}'
}

restore_snapshot() {
    local operation_lease_id="" status_response vm_status
    check_required_fields snap_name
    validate_name "Snapshot" "$snap_name"

    if is_adoption; then
        require_managed_adoption || return 1
        operation_lease_id=$(begin_managed_adoption_operation restore_snapshot) || {
            printf '%s\n' "$operation_lease_id"
            return 1
        }
    fi

    execute_and_wait POST "/nodes/${node}/qemu/${vmid}/snapshot/${snap_name}/rollback"

    if ! status_response=$(call_proxmox_api GET "/nodes/${node}/qemu/${vmid}/status/current"); then
        echo '{"status":"error","message":"Could not verify VM state after snapshot restore"}'
        return 1
    fi
    if ! vm_status=$(echo "$status_response" | jq -er '.data.status | select(. == "running" or . == "stopped")' 2>/dev/null); then
        echo '{"status":"error","message":"Could not verify VM state after snapshot restore"}'
        return 1
    fi
    if [ "$vm_status" = "stopped" ];then
        execute_and_wait POST "/nodes/${node}/qemu/${vmid}/status/start"
    fi

    if [[ -n "$operation_lease_id" ]]; then
        complete_managed_adoption_operation restore_snapshot "$operation_lease_id" || return 1
    fi
    echo '{"status": "success", "message": "Instance Snapshot restored"}'
}

delete_snapshot() {
    local operation_lease_id=""
    check_required_fields snap_name
    validate_name "Snapshot" "$snap_name"

    if is_adoption; then
        require_managed_adoption || return 1
        operation_lease_id=$(begin_managed_adoption_operation delete_snapshot) || {
            printf '%s\n' "$operation_lease_id"
            return 1
        }
    fi

    execute_and_wait DELETE "/nodes/${node}/qemu/${vmid}/snapshot/${snap_name}"
    if [[ -n "$operation_lease_id" ]]; then
        complete_managed_adoption_operation delete_snapshot "$operation_lease_id" || return 1
    fi
    echo '{"status": "success", "message": "Instance Snapshot deleted"}'
}

action=$1
parameters_file="$2"
wait_time=$3

if [[ -z "$action" || -z "$parameters_file" ]]; then
    echo '{"status": "error", "error": "Missing required arguments"}'
    exit 1
fi

if [[ ! -r "$parameters_file" ]]; then
    echo '{"status": "error", "error": "File not found or unreadable"}'
    exit 1
fi

# Read file content as parameters (assumes space-separated arguments)
parameters=$(<"$parameters_file")

parse_json "$parameters" || exit 1

cleanup_vm=0
# shellcheck disable=SC2317,SC2329  # invoked indirectly by EXIT trap
cleanup() {
    if (( cleanup_vm == 1 )) && ! is_adoption; then
        execute_and_wait DELETE "/nodes/${node}/qemu/${vmid}"
    fi
}

trap cleanup EXIT

dispatch_action() {
    case $action in
        prepare)
            prepare
            ;;
        create)
            create
            ;;
        delete)
            delete
            ;;
        start)
            start
            ;;
        stop)
            stop
            ;;
        reboot)
            reboot
            ;;
        status)
            status
            ;;
        statuses)
            statuses
            ;;
        getconsole)
            get_console
            ;;
        ListSnapshots)
            list_snapshots
            ;;
        CreateSnapshot)
            create_snapshot
            ;;
        RestoreSnapshot)
            restore_snapshot
            ;;
        DeleteSnapshot)
            delete_snapshot
            ;;
        *)
            echo '{"status": "error", "error": "Invalid action"}'
            return 1
            ;;
    esac
}

dispatch_action
exit $?
