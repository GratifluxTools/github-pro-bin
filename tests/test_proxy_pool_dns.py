import importlib.util
import pathlib
import unittest
from unittest.mock import call, patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("proxy_pool_dns", ROOT / ".github/scripts/proxy_pool.py")
module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(module)

ZONE = {"success": True, "result": {"id": "zone-1", "name": "rokko-st.com"}}
OK = {"success": True, "result": {"id": "created-cname"}}


class CloudflarePoolDNSTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict("os.environ", {"CF_API_TOKEN": "token", "CF_ZONE_ID": "zone-1"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_handoff_updates_live_cname_id_not_original_a_record_id(self):
        current = [{"id": "temporary-cname-id", "type": "CNAME", "name": "proxy1.rokko-st.com", "content": "old.run.pinggy-free.link"}]
        with patch.object(module, "cf_request", side_effect=[(200, ZONE), (200, {"success": True, "result": current}), (200, OK)]) as api:
            dns = module.CloudflareDNS()
            dns.point("proxy1.rokko-st.com", "new.run.pinggy-free.link", {
                "record": {"type": "A", "name": "proxy1.rokko-st.com", "content": "192.0.2.1"},
                "record_id": "old-a-id",
            })
        self.assertEqual(api.call_args_list[2], call("PUT", "/zones/zone-1/dns_records/temporary-cname-id", "token", unittest.mock.ANY))

    def test_removes_only_the_snapshotted_a_record_before_creating_cname(self):
        current = [{"id": "old-a-id", "type": "A", "name": "proxy2.rokko-st.com", "content": "192.0.2.8"}]
        with patch.object(module, "cf_request", side_effect=[
            (200, ZONE), (200, {"success": True, "result": current}),
            (200, {"success": True}), (201, OK),
        ]) as api:
            dns = module.CloudflareDNS()
            dns.point("proxy2.rokko-st.com", "new.run.pinggy-free.link", {
                "record": {"type": "A", "name": "proxy2.rokko-st.com", "content": "192.0.2.8"},
                "record_id": "old-a-id",
            })
        self.assertEqual(api.call_args_list[2].args[0], "DELETE")
        self.assertEqual(api.call_args_list[3].args[0], "POST")

    def test_refuses_to_replace_unexpected_cname(self):
        current = [{"id": "external-id", "type": "CNAME", "name": "proxy1.rokko-st.com", "content": "someone-else.example.net"}]
        with patch.object(module, "cf_request", side_effect=[(200, ZONE), (200, {"success": True, "result": current})]) as api:
            dns = module.CloudflareDNS()
            with self.assertRaisesRegex(RuntimeError, "unexpected CNAME"):
                dns.point("proxy1.rokko-st.com", "new.run.pinggy-free.link", {"record": None, "record_id": None})
        self.assertEqual(api.call_count, 2)

    def test_restores_deleted_a_record_if_temporary_cname_creation_fails(self):
        original = {"type": "A", "name": "proxy3.rokko-st.com", "content": "192.0.2.5", "ttl": 300, "proxied": True}
        current = [{"id": "old-a-id", **original}]
        with patch.object(module, "cf_request", side_effect=[
            (200, ZONE), (200, {"success": True, "result": current}),
            (200, {"success": True}), (400, {"success": False, "errors": [{"message": "create failed"}]}),
            (201, {"success": True, "result": {"id": "restored-a"}}),
        ]) as api:
            dns = module.CloudflareDNS()
            with self.assertRaisesRegex(RuntimeError, "create Pinggy CNAME"):
                dns.point("proxy3.rokko-st.com", "new.run.pinggy-free.link", {"record": original, "record_id": "old-a-id"})
        self.assertEqual(api.call_args_list[4].args[0], "POST")
        self.assertEqual(api.call_args_list[4].args[3]["type"], "A")


if __name__ == "__main__":
    unittest.main()
