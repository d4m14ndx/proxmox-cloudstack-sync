import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from cloudstack_client import CloudStackClient
from config import CloudStackConfig


class CloudStackClientTransportTests(unittest.TestCase):
    def setUp(self):
        self.client = CloudStackClient(
            CloudStackConfig(
                url="https://cloudstack.invalid/client/api",
                api_key="test-api-key",
                secret_key="test-secret-key",
            )
        )

    @staticmethod
    def response(payload):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = payload
        return response

    def test_signing_does_not_mutate_input(self):
        params = {"command": "listZones", "listall": "true"}
        original = dict(params)
        signature = self.client._sign(params)
        self.assertIsInstance(signature, str)
        self.assertEqual(original, params)

    @patch("cloudstack_client.requests.get")
    @patch("cloudstack_client.requests.post")
    def test_mutations_use_signed_form_post_not_query_parameters(self, post, get):
        post.side_effect = [
            self.response({"deployvirtualmachineresponse": {"jobid": "deploy-job"}}),
            self.response({"startvirtualmachineresponse": {"jobid": "start-job"}}),
            self.response({"destroyvirtualmachineresponse": {"jobid": "destroy-job"}}),
        ]

        self.assertEqual(
            "deploy-job",
            self.client.deploy_virtual_machine(
                customid="10000000-0000-4000-8000-000000000001",
                startvm="false",
                externaldetails="non-secret-manifest",
            )["jobid"],
        )
        self.assertEqual(
            "start-job",
            self.client.start_virtual_machine(
                "10000000-0000-4000-8000-000000000001"
            )["jobid"],
        )
        self.assertEqual(
            "destroy-job",
            self.client.destroy_virtual_machine(
                "10000000-0000-4000-8000-000000000001"
            )["jobid"],
        )

        get.assert_not_called()
        self.assertEqual(3, post.call_count)
        for call in post.call_args_list:
            self.assertEqual(self.client.url, call.args[0])
            self.assertNotIn("params", call.kwargs)
            self.assertIn("data", call.kwargs)
            self.assertIn("signature", call.kwargs["data"])
            self.assertEqual(30, call.kwargs["timeout"])
        deploy_data = post.call_args_list[0].kwargs["data"]
        self.assertEqual("deployVirtualMachine", deploy_data["command"])
        self.assertEqual("non-secret-manifest", deploy_data["externaldetails"])

    @patch("cloudstack_client.requests.get")
    @patch("cloudstack_client.requests.post")
    def test_read_queries_remain_get(self, post, get):
        get.side_effect = [
            self.response({"listvirtualmachinesresponse": {"virtualmachine": []}}),
            self.response({"queryasyncjobresultresponse": {"jobstatus": 0}}),
        ]
        self.assertEqual([], self.client.list_virtual_machines())
        self.assertEqual({"jobstatus": 0}, self.client.query_async_job("job-1"))
        post.assert_not_called()
        self.assertEqual(2, get.call_count)
        for call in get.call_args_list:
            self.assertIn("params", call.kwargs)
            self.assertNotIn("data", call.kwargs)

    @patch("cloudstack_client.requests.get")
    def test_vm_inventory_page_limit_stops_nonterminating_full_pages(self, get):
        full_page = self.response(
            {"listvirtualmachinesresponse": {"virtualmachine": [{"id": "vm"}]}}
        )
        get.return_value = full_page

        with self.assertRaisesRegex(RuntimeError, "page limit exceeded"):
            self.client.list_virtual_machines(pagesize=1, _max_pages=2)

        self.assertEqual(2, get.call_count)


if __name__ == "__main__":
    unittest.main()
