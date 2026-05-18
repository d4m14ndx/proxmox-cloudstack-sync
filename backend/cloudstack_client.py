import hashlib
import hmac
import base64
import urllib.parse
import requests
import logging
from config import CloudStackConfig

log = logging.getLogger(__name__)


class CloudStackClient:
    def __init__(self, config: CloudStackConfig):
        self.url = config.url
        self.api_key = config.api_key
        self.secret_key = config.secret_key

    def _sign(self, params: dict) -> str:
        params["apiKey"] = self.api_key
        params["response"] = "json"

        sorted_params = sorted(params.items(), key=lambda x: x[0].lower())
        query = "&".join(
            f"{k.lower()}={urllib.parse.quote(str(v), safe='*')}"
            for k, v in sorted_params
        )

        sig = hmac.new(
            self.secret_key.encode("utf-8"),
            query.lower().encode("utf-8"),
            hashlib.sha1,
        ).digest()
        return base64.b64encode(sig).decode("utf-8")

    def request(self, command: str, **params) -> dict:
        params["command"] = command
        params["apiKey"] = self.api_key
        params["response"] = "json"
        signature = self._sign(params)
        params["signature"] = signature

        try:
            resp = requests.get(self.url, params=params, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.error(f"CloudStack API error ({command}): {e}")
            raise

    def list_virtual_machines(self, **kwargs) -> list[dict]:
        vms = []
        page = 1
        page_size = kwargs.pop("pagesize", 50)
        while True:
            result = self.request(
                "listVirtualMachines",
                listall="true",
                page=str(page),
                pagesize=str(page_size),
                **kwargs,
            )
            batch = result.get("listvirtualmachinesresponse", {}).get("virtualmachine", [])
            if not batch:
                break
            vms.extend(batch)
            if len(batch) < page_size:
                break
            page += 1
        return vms

    def list_hosts(self, **kwargs) -> list[dict]:
        result = self.request("listHosts", listall="true", type="Routing", **kwargs)
        return result.get("listhostsresponse", {}).get("host", [])

    def list_clusters(self, **kwargs) -> list[dict]:
        result = self.request("listClusters", listall="true", **kwargs)
        return result.get("listclustersresponse", {}).get("cluster", [])

    def list_zones(self, **kwargs) -> list[dict]:
        result = self.request("listZones", listall="true", **kwargs)
        return result.get("listzonesresponse", {}).get("zone", [])

    def list_service_offerings(self, **kwargs) -> list[dict]:
        result = self.request("listServiceOfferings", listall="true", **kwargs)
        return result.get("listserviceofferingsresponse", {}).get("serviceoffering", [])

    def list_networks(self, **kwargs) -> list[dict]:
        result = self.request("listNetworks", listall="true", **kwargs)
        return result.get("listnetworksresponse", {}).get("network", [])

    def list_disk_offerings(self, **kwargs) -> list[dict]:
        result = self.request("listDiskOfferings", listall="true", **kwargs)
        return result.get("listdiskofferingsresponse", {}).get("diskoffering", [])

    def list_unmanaged_instances(self, cluster_id: str, **kwargs) -> list[dict]:
        result = self.request(
            "listUnmanagedInstances", clusterid=cluster_id, **kwargs
        )
        return result.get("listunmanagedinstancesresponse", {}).get("unmanagedinstance", [])

    def import_unmanaged_instance(self, **params) -> dict:
        result = self.request("importUnmanagedInstance", **params)
        return result.get("importunmanagedinstanceresponse", {})

    def query_async_job(self, job_id: str) -> dict:
        result = self.request("queryAsyncJobResult", jobid=job_id)
        return result.get("queryasyncjobresultresponse", {})
