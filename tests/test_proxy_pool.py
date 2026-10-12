import importlib.util
import pathlib
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("proxy_pool", ROOT / ".github/scripts/proxy_pool.py")
module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(module)


class ProxyPoolTests(unittest.TestCase):
    def test_parses_real_pinggy_endpoint(self):
        sample = "tcp://cznis-20-119-78-149.run.pinggy-free.link:41639"
        match = module.ENDPOINT_RE.search(sample)
        self.assertIsNotNone(match)
        assert match is not None
        host = match.group("host")
        self.assertEqual(host, "cznis-20-119-78-149.run.pinggy-free.link")
        self.assertTrue(host.endswith(module.PINGGY_SUFFIXES))
        self.assertEqual(match.group("port"), "41639")

    def test_public_payload_includes_only_live_slots_sorted_and_no_credentials(self):
        expiry = (datetime.now(timezone.utc) + timedelta(minutes=35)).isoformat().replace("+00:00", "Z")
        state = {
            "schema": 1,
            "slots": {
                "3": {"status": "online", "server": "proxy3.rokko-st.com", "port": 4567,
                      "speed_mbps": 8.2, "connections": 2, "expires_at": expiry,
                      "run_id": "secret-run", "PROXY_PASS": "must-not-publish"},
                "1": {"status": "starting", "server": "proxy1.rokko-st.com", "port": 4444,
                      "expires_at": expiry},
            },
        }
        result = module.public_payload(state)
        self.assertEqual([p["slot"] for p in result["proxies"]], [3])
        self.assertEqual(result["proxies"][0]["port"], 4567)
        self.assertEqual(result["refresh_seconds"], 600)
        self.assertNotIn("PROXY_PASS", str(result))
        self.assertNotIn("secret-run", str(result))

    def test_expired_slot_is_not_published(self):
        expired = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
        state = {"slots": {"1": {"status": "online", "server": "proxy1.rokko-st.com", "port": 41639, "expires_at": expired}}}
        self.assertEqual(module.public_payload(state)["proxies"], [])

    def test_state_update_retries_sha_conflict(self):
        state = {"schema": 1, "slots": {}}
        with patch.object(module, "read_state", side_effect=[(state.copy(), "old"), (state.copy(), "new")]) as reader, \
             patch.object(module, "write_state", side_effect=[False, True]) as writer, \
             patch.object(module.time, "sleep"):
            result = module.update_state("owner/repo", "token", "test", lambda data: (True, "done"))
        self.assertEqual(result, "done")
        self.assertEqual(reader.call_count, 2)
        self.assertEqual(writer.call_count, 2)

    def test_successor_dispatch_targets_dedicated_slot_workflow(self):
        with patch.object(module, "github_api", return_value=(204, {})) as api:
            module.dispatch_successor("owner/repo", "control-token", 2, "123456")
        self.assertEqual(api.call_args.args[2], "actions/workflows/proxy-slot-2.yml/dispatches")
        self.assertEqual(api.call_args.args[3], "POST")
        self.assertEqual(api.call_args.args[4]["inputs"]["handoff_from"], "123456")


if __name__ == "__main__":
    unittest.main()
