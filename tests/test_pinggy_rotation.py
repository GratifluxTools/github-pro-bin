import importlib.util
import pathlib
import unittest
from unittest.mock import call, patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "pinggy_rotation", ROOT / ".github/scripts/pinggy_rotation.py"
)
module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(module)


class DNSManagerTests(unittest.TestCase):
    def test_updates_existing_cname_dns_only_and_restores_original(self):
        record = {
            "id": "record-1",
            "type": "CNAME",
            "name": "proxy.rokko-st.com",
            "content": "old.example.net",
            "ttl": 1,
            "proxied": True,
        }
        with patch.object(module, "cf_request", side_effect=[{"result": [record]}, {}, {}]) as api:
            dns = module.DNSManager()
            dns.update("t-123.a.pinggy.io")
            dns.restore()

        update_payload = api.call_args_list[1].args[2]
        self.assertEqual(update_payload["content"], "t-123.a.pinggy.io")
        self.assertFalse(update_payload["proxied"])
        self.assertEqual(update_payload["ttl"], 60)
        restore_payload = api.call_args_list[2].args[2]
        self.assertEqual(restore_payload["content"], "old.example.net")
        self.assertTrue(restore_payload["proxied"])

    def test_creates_temporary_record_and_deletes_on_restore(self):
        with patch.object(
            module,
            "cf_request",
            side_effect=[{"result": []}, {"result": {"id": "new-record"}}, {}],
        ) as api:
            dns = module.DNSManager()
            dns.update("t-123.a.pinggy.io")
            dns.restore()
        self.assertEqual(api.call_args_list[1].args[0], "POST")
        self.assertEqual(api.call_args_list[2], call("DELETE", "/dns_records/new-record"))

    def test_temporarily_replaces_single_a_record_and_recreates_it_on_restore(self):
        record = {
            "id": "record-a",
            "type": "A",
            "name": "proxy.rokko-st.com",
            "content": "192.0.2.10",
            "ttl": 1,
            "proxied": True,
        }
        with patch.object(
            module,
            "cf_request",
            side_effect=[{"result": [record]}, {}, {"result": {"id": "temporary-cname"}}, {}, {"result": {"id": "restored-a"}}],
        ) as api:
            dns = module.DNSManager()
            dns.update("t-123.a.pinggy.io")
            dns.restore()
        self.assertEqual(api.call_args_list[1], call("DELETE", "/dns_records/record-a"))
        self.assertEqual(api.call_args_list[2].args[0], "POST")
        self.assertEqual(api.call_args_list[2].args[2]["type"], "CNAME")
        restore_payload = api.call_args_list[4].args[2]
        self.assertEqual(restore_payload["type"], "A")
        self.assertEqual(restore_payload["content"], "192.0.2.10")
        self.assertTrue(restore_payload["proxied"])

    def test_fails_closed_on_conflicting_record_types(self):
        records = [
            {"id": "record-a", "type": "A", "name": "proxy.rokko-st.com", "content": "192.0.2.5"},
            {"id": "record-aaaa", "type": "AAAA", "name": "proxy.rokko-st.com", "content": "2001:db8::1"},
        ]
        with patch.object(module, "cf_request", return_value={"result": records}):
            with self.assertRaisesRegex(RuntimeError, "expected exactly one A or CNAME"):
                module.DNSManager()

    def test_extracts_pinggy_host_and_assigned_port(self):
        match = module.PINGGY_ENDPOINT.search("Forwarding TCP: tcp://t-123.a.pinggy.io:45678")
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.group("host"), "t-123.a.pinggy.io")
        self.assertEqual(match.group("port"), "45678")


if __name__ == "__main__":
    unittest.main()
