import importlib.util
import pathlib
import unittest
from unittest.mock import call, patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "pinggy_dns_proxy", ROOT / ".github/scripts/pinggy_dns_proxy.py"
)
module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(module)

ZONE = {"success": True, "result": {"id": "zone-1", "name": "rokko-st.com"}}


def zone_and_records(records):
    return [(200, ZONE), (200, {"success": True, "result": records})]


class DNSManagerTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict("os.environ", {"CF_API_TOKEN": "test-token", "CF_ZONE_ID": "zone-1"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_updates_existing_cname_to_dns_only_and_restores_it(self):
        record = {
            "id": "record-cname",
            "type": "CNAME",
            "name": "proxy.rokko-st.com",
            "content": "old-tunnel.cfargotunnel.com",
            "ttl": 1,
            "proxied": True,
            "comment": "original",
        }
        with patch.object(module, "cf_request", side_effect=zone_and_records([record]) + [(200, {"success": True}), (200, {"success": True})]) as api:
            dns = module.DNSManager()
            dns.update("cznis.run.pinggy-free.link")
            dns.restore()
        update = api.call_args_list[2]
        self.assertEqual(update.args[0], "PUT")
        self.assertEqual(update.args[3]["content"], "cznis.run.pinggy-free.link")
        self.assertFalse(update.args[3]["proxied"])
        restore = api.call_args_list[3]
        self.assertEqual(restore.args[3]["content"], "old-tunnel.cfargotunnel.com")
        self.assertTrue(restore.args[3]["proxied"])

    def test_temporarily_replaces_a_record_and_restores_it(self):
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
            side_effect=zone_and_records([record]) + [
                (200, {"success": True}),
                (200, {"success": True, "result": {"id": "new-cname"}}),
                (200, {"success": True}),
                (200, {"success": True, "result": {"id": "restored-a"}}),
            ],
        ) as api:
            dns = module.DNSManager()
            dns.update("cznis.run.pinggy-free.link")
            dns.restore()
        self.assertEqual(api.call_args_list[2], call("DELETE", "/zones/zone-1/dns_records/record-a", "test-token"))
        self.assertEqual(api.call_args_list[3].args[3]["type"], "CNAME")
        self.assertEqual(api.call_args_list[5].args[3]["type"], "A")
        self.assertTrue(api.call_args_list[5].args[3]["proxied"])

    def test_creates_and_removes_temporary_cname_when_no_prior_record(self):
        with patch.object(
            module,
            "cf_request",
            side_effect=zone_and_records([]) + [
                (201, {"success": True, "result": {"id": "temp"}}),
                (200, {"success": True}),
            ],
        ) as api:
            dns = module.DNSManager()
            dns.update("cznis.run.pinggy-free.link")
            dns.restore()
        self.assertEqual(api.call_args_list[2].args[0], "POST")
        self.assertEqual(api.call_args_list[3].args[0], "DELETE")

    def test_rejects_conflicting_records_without_mutating_them(self):
        records = [
            {"id": "a", "type": "A", "name": "proxy.rokko-st.com"},
            {"id": "aaaa", "type": "AAAA", "name": "proxy.rokko-st.com"},
        ]
        with patch.object(module, "cf_request", side_effect=zone_and_records(records)) as api:
            with self.assertRaisesRegex(RuntimeError, "expected at most one A/CNAME"):
                module.DNSManager()
        self.assertEqual(api.call_count, 2)

    def test_parses_actual_pinggy_free_tcp_url(self):
        sample = "tcp://cznis-20-119-78-149.run.pinggy-free.link:41639"
        match = module.ENDPOINT_RE.search(sample)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.group("host"), "cznis-20-119-78-149.run.pinggy-free.link")
        self.assertEqual(match.group("port"), "41639")
        self.assertTrue(match.group("host").endswith(module.PINGGY_SUFFIXES))


if __name__ == "__main__":
    unittest.main()
