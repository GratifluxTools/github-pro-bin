#!/usr/bin/env python3
"""Manage one of three Pinggy Free proxies and publish a public status JSON."""
from __future__ import annotations

import base64
import json
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = "proxy-pool.json"
ZONE = "rokko-st.com"
TTL = 60
LEASE_SECONDS = 15 * 60
PINGGY_LIFETIME = 60 * 60
HANDOFF_AFTER = 55 * 60
HEARTBEAT = 10 * 60
HANDOFF_WAIT = 8 * 60
PINGGY_SUFFIXES = (".run.pinggy-free.link", ".pinggy.io", ".pinggy.link")
ENDPOINT_RE = re.compile(r"tcp://(?P<host>[A-Za-z0-9.-]+):(?P<port>[0-9]{1,5})")
STOP = threading.Event()
CHILD_GROUPS: set[int] = set()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def api_request(url: str, token: str, method: str = "GET", body: dict | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "github-pro-bin-proxy-pool",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        try:
            return exc.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return exc.code, {"message": raw[:1200]}
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GitHub API request failed: {exc.reason}") from exc


def github_context() -> tuple[str, str, str]:
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    run_id = os.environ.get("GITHUB_RUN_ID", "").strip()
    if not repo or not token or not run_id:
        raise RuntimeError("Missing GITHUB_REPOSITORY, GITHUB_TOKEN, or GITHUB_RUN_ID")
    return repo, token, run_id


def github_api(repo: str, token: str, path: str, method: str = "GET", body: dict | None = None) -> tuple[int, dict]:
    return api_request(f"https://api.github.com/repos/{repo}/{path.lstrip('/')}", token, method, body)


def read_state(repo: str, token: str) -> tuple[dict, str | None]:
    status, result = github_api(repo, token, f"contents/{STATE_PATH}?ref=main")
    if status == 404:
        return {"schema": 1, "updated_at": now_iso(), "slots": {}}, None
    if status != 200 or "content" not in result:
        raise RuntimeError(f"Cannot read {STATE_PATH} (HTTP {status}): {result.get('message', result.get('errors'))}")
    try:
        raw = base64.b64decode(result["content"], validate=False).decode("utf-8")
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid JSON in {STATE_PATH}: {exc}") from exc
    if data.get("schema") != 1 or not isinstance(data.get("slots"), dict):
        raise RuntimeError(f"Unsupported or malformed {STATE_PATH}")
    return data, result.get("sha")


def write_state(repo: str, token: str, data: dict, sha: str | None, message: str) -> bool:
    public = public_payload(data)
    published_state = {
        **data,
        "generated_at": public["generated_at"],
        "refresh_seconds": public["refresh_seconds"],
        "proxies": public["proxies"],
    }
    body: dict = {
        "message": message,
        "content": base64.b64encode((json.dumps(published_state, ensure_ascii=False, indent=2) + "\n").encode()).decode(),
        "branch": "main",
    }
    if sha:
        body["sha"] = sha
    status, result = github_api(repo, token, f"contents/{STATE_PATH}", "PUT", body)
    if status in (409, 422):
        return False
    if status not in (200, 201):
        raise RuntimeError(f"Cannot update {STATE_PATH} (HTTP {status}): {result.get('message', result.get('errors'))}")
    return True


def update_state(repo: str, token: str, message: str, mutate, attempts: int = 12):
    """Optimistic compare-and-swap using the GitHub Contents API file SHA."""
    for attempt in range(attempts):
        state, sha = read_state(repo, token)
        changed, value = mutate(state)
        if not changed:
            return value
        state["schema"] = 1
        state["updated_at"] = now_iso()
        if write_state(repo, token, state, sha, message):
            return value
        time.sleep(min(0.25 * (attempt + 1), 2))
    raise RuntimeError(f"Could not update {STATE_PATH}: repeated concurrent file changes")


def list_active_runs(repo: str, token: str) -> set[str]:
    active: set[str] = set()
    for page in range(1, 6):
        status, response = github_api(repo, token, f"actions/runs?status=in_progress&per_page=100&page={page}")
        if status != 200:
            print(f"[WARN] unable to reconcile stale reservations (HTTP {status})", flush=True)
            return active
        runs = response.get("workflow_runs", [])
        active.update(str(run.get("id")) for run in runs)
        if len(runs) < 100:
            break
    return active


def cf_request(method: str, path: str, token: str, body: dict | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "github-pro-bin-proxy-pool"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        try:
            return exc.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return exc.code, {"errors": [{"message": raw[:1000]}]}
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Cloudflare API connection failed: {exc.reason}") from exc


def cf_ok(status: int, result: dict, label: str) -> dict:
    if not 200 <= status < 300 or not result.get("success"):
        raise RuntimeError(f"Cloudflare {label} failed (HTTP {status}): {result.get('errors', [])}")
    return result


class CloudflareDNS:
    def __init__(self) -> None:
        self.token = os.environ.get("CF_API_TOKEN", "").strip()
        zone_id = os.environ.get("CF_ZONE_ID", "").strip()
        if not self.token or not zone_id:
            raise RuntimeError("Set CF_API_TOKEN and CF_ZONE_ID Actions secrets")
        status, result = cf_request("GET", f"/zones/{urllib.parse.quote(zone_id, safe='')}", self.token)
        if status == 200 and result.get("success") and result.get("result", {}).get("name", "").lower() == ZONE:
            self.zone_id = result["result"]["id"]
        elif any(err.get("code") == 9109 for err in result.get("errors", []) if isinstance(err, dict)):
            query = urllib.parse.urlencode({"name": ZONE, "per_page": 50})
            status, found = cf_request("GET", f"/zones?{query}", self.token)
            matches = [z for z in cf_ok(status, found, "zone lookup").get("result", []) if z.get("name", "").lower() == ZONE]
            if len(matches) != 1:
                raise RuntimeError(f"Expected exactly one accessible Cloudflare zone named {ZONE}; found {len(matches)}")
            self.zone_id = matches[0]["id"]
        else:
            raise RuntimeError(f"Cloudflare zone verification failed (HTTP {status}): {result.get('errors', [])}")

    @staticmethod
    def payload(record: dict) -> dict:
        return {k: record[k] for k in ("type", "name", "content", "ttl", "proxied", "comment", "tags", "settings") if k in record}

    def records(self, name: str) -> list[dict]:
        query = urllib.parse.urlencode({"name": name, "per_page": 100})
        status, result = cf_request("GET", f"/zones/{self.zone_id}/dns_records?{query}", self.token)
        values = cf_ok(status, result, "DNS lookup").get("result", [])
        return [r for r in values if r.get("name", "").rstrip(".").lower() == name.lower()]

    def snapshot(self, name: str) -> dict:
        records = self.records(name)
        if len(records) > 1 or (records and records[0].get("type") not in ("A", "CNAME")):
            types = ", ".join(str(r.get("type")) for r in records)
            raise RuntimeError(f"Refusing to use {name}: expected at most one A/CNAME record, found {types or 'unsupported record'}")
        record = records[0] if records else None
        return {"record": self.payload(record) if record else None, "record_id": record.get("id") if record else None}

    def point(self, name: str, host: str, baseline: dict) -> None:
        record = baseline.get("record")
        record_id = baseline.get("record_id")
        desired = {
            "type": "CNAME", "name": name, "content": host, "ttl": TTL, "proxied": False,
            "comment": "GitHub Actions Pinggy Free proxy pool (temporary)",
        }
        current = self.records(name)
        restore_a_on_failure = False
        if len(current) > 1 or (current and current[0].get("type") not in ("A", "CNAME")):
            raise RuntimeError(f"Refusing to change {name}: conflicting DNS records appeared after reservation")
        if current and current[0].get("type") == "CNAME":
            # The previous Pinggy generation may have created a new CNAME ID when
            # replacing an A record; always update the live record, not the baseline ID.
            is_original = bool(
                record and record.get("type") == "CNAME"
                and current[0].get("id") == record_id
                and current[0].get("content") == record.get("content")
            )
            is_pinggy = str(current[0].get("content", "")).rstrip(".").lower().endswith(PINGGY_SUFFIXES)
            if not is_original and not is_pinggy:
                raise RuntimeError(f"Refusing to overwrite unexpected CNAME for {name}")
            status, result = cf_request("PUT", f"/zones/{self.zone_id}/dns_records/{current[0]['id']}", self.token, desired)
        else:
            if current and current[0].get("type") == "A":
                if (not record or record.get("type") != "A"
                    or current[0].get("id") != record_id
                    or current[0].get("content") != record.get("content")):
                    raise RuntimeError(f"Refusing to remove unexpected A record for {name}")
                status, result = cf_request("DELETE", f"/zones/{self.zone_id}/dns_records/{current[0]['id']}", self.token)
                cf_ok(status, result, f"remove current A record for {name}")
                restore_a_on_failure = True
            elif not current and record and record.get("type") == "A" and record_id:
                # A previous interrupted generation may already have removed it.
                status, result = cf_request("DELETE", f"/zones/{self.zone_id}/dns_records/{record_id}", self.token)
                if status not in (200, 404):
                    cf_ok(status, result, f"remove original A record for {name}")
                restore_a_on_failure = True
            try:
                status, result = cf_request("POST", f"/zones/{self.zone_id}/dns_records", self.token, desired)
                cf_ok(status, result, f"create Pinggy CNAME for {name}")
            except Exception:
                if restore_a_on_failure and record and record.get("type") == "A":
                    restore_status, restore_result = cf_request("POST", f"/zones/{self.zone_id}/dns_records", self.token, record)
                    cf_ok(restore_status, restore_result, f"rollback original A record for {name}")
                raise
        if current and current[0].get("type") == "CNAME":
            cf_ok(status, result, f"point {name} to Pinggy")
        print(f"[DNS] {name} CNAME -> {host} (DNS-only)", flush=True)

    def restore_if_owned(self, name: str, expected_host: str, baseline: dict) -> None:
        current = self.records(name)
        if len(current) != 1 or current[0].get("type") != "CNAME" or current[0].get("content", "").rstrip(".").lower() != expected_host.rstrip(".").lower():
            print(f"[DNS] left {name} unchanged: current DNS no longer points to this run", flush=True)
            return
        current_id = current[0]["id"]
        original = baseline.get("record")
        if original is None:
            status, result = cf_request("DELETE", f"/zones/{self.zone_id}/dns_records/{current_id}", self.token)
            cf_ok(status, result, f"remove temporary record for {name}")
        elif original.get("type") == "CNAME":
            status, result = cf_request("PUT", f"/zones/{self.zone_id}/dns_records/{current_id}", self.token, original)
            cf_ok(status, result, f"restore CNAME for {name}")
        elif original.get("type") == "A":
            status, result = cf_request("DELETE", f"/zones/{self.zone_id}/dns_records/{current_id}", self.token)
            cf_ok(status, result, f"remove temporary CNAME for {name}")
            status, result = cf_request("POST", f"/zones/{self.zone_id}/dns_records", self.token, original)
            cf_ok(status, result, f"restore A record for {name}")
        print(f"[DNS] restored original DNS state for {name}", flush=True)

    def restore_if_pinggy(self, name: str, baseline: dict) -> None:
        """Restore a slot after cancellation even if a pending successor changed its target."""
        records = self.records(name)
        original = baseline.get("record") or {}
        if not records and original.get("type") == "A":
            status, result = cf_request("POST", f"/zones/{self.zone_id}/dns_records", self.token, original)
            cf_ok(status, result, f"restore original A record for {name}")
            print(f"[DNS] restored original A record for {name}", flush=True)
            return
        if len(records) != 1 or records[0].get("type") != "CNAME":
            return
        current_host = str(records[0].get("content", "")).rstrip(".").lower()
        if current_host.endswith(PINGGY_SUFFIXES):
            self.restore_if_owned(name, current_host, baseline)



class Tunnel:
    def __init__(self) -> None:
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.host: str | None = None
        self.port: int | None = None
        self.proc = subprocess.Popen(
            ["ssh", "-T", "-o", "StrictHostKeyChecking=no", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3", "-o", "ConnectTimeout=20", "-o", "ExitOnForwardFailure=yes", "-p", "443", "-R", "0:127.0.0.1:8080", "tcp@free.pinggy.io"],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, start_new_session=True,
        )
        CHILD_GROUPS.add(self.proc.pid)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        assert self.proc.stdout
        for line in self.proc.stdout:
            line = line.rstrip("\r\n")
            print(f"[Pinggy] {line}", flush=True)
            match = ENDPOINT_RE.search(line)
            if match:
                host = match.group("host").lower().rstrip(".")
                port = int(match.group("port"))
                if host.endswith(PINGGY_SUFFIXES) and 1 <= port <= 65535:
                    self.host, self.port = host, port
            self.lines.put(line)
        self.lines.put(None)

    def wait_endpoint(self, timeout: int = 100) -> None:
        deadline = time.monotonic() + timeout
        while not STOP.is_set() and time.monotonic() < deadline:
            if self.host and self.port:
                return
            if self.proc.poll() is not None:
                break
            try:
                self.lines.get(timeout=0.5)
            except queue.Empty:
                pass
        raise RuntimeError(f"Pinggy did not provide a recognized TCP endpoint (ssh exit={self.proc.poll()})")

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.proc.wait(timeout=7)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.proc.wait(timeout=3)
        CHILD_GROUPS.discard(self.proc.pid)
        self.reader.join(timeout=1)


def signal_handler(signum: int, _frame) -> None:
    print(f"[STOP] received signal {signum}", flush=True)
    STOP.set()
    for group in tuple(CHILD_GROUPS):
        try:
            os.killpg(group, signal.SIGTERM)
        except ProcessLookupError:
            pass


def run_id() -> str:
    return os.environ["GITHUB_RUN_ID"]


def slot_key(number: int) -> str:
    return str(number)


def public_payload(state: dict) -> dict:
    proxies = []
    now = datetime.now(timezone.utc)
    for number, entry in state.get("slots", {}).items():
        if entry.get("status") != "online":
            continue
        expires = parse_time(entry.get("expires_at"))
        if not expires or expires <= now:
            continue
        proxies.append({
            "slot": int(number), "server": entry.get("server"), "port": entry.get("port"),
            "speed_mbps": entry.get("speed_mbps"), "connections": entry.get("connections"),
            "started_at": entry.get("started_at"), "expires_at": entry.get("expires_at"),
            "updated_at": entry.get("updated_at"), "status": "online",
        })
    return {"schema": 1, "generated_at": now_iso(), "refresh_seconds": HEARTBEAT, "proxies": sorted(proxies, key=lambda x: x["slot"]), "slots": 3}


def publish(repo: str, token: str, message: str) -> None:
    update_state(repo, token, message, lambda state: (True, None))


def active_connections() -> int | None:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8081/metrics", timeout=3) as response:
            payload = json.loads(response.read())
        value = payload.get("active_connections")
        return max(0, int(value)) if value is not None else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def speed_test() -> float | None:
    user = urllib.parse.quote(os.environ.get("PROXY_USER", ""), safe="")
    password = urllib.parse.quote(os.environ.get("PROXY_PASS", ""), safe="")
    if not user or not password:
        return None
    proxy = f"http://{user}:{password}@127.0.0.1:8080"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    request = urllib.request.Request("https://speed.cloudflare.com/__down?bytes=1000000", headers={"User-Agent": "github-pro-bin-proxy-health/1.0"})
    started = time.monotonic()
    try:
        with opener.open(request, timeout=25) as response:
            body = response.read(1_100_000)
        elapsed = max(0.05, time.monotonic() - started)
        if not body:
            return None
        return round((len(body) * 8 / elapsed) / 1_000_000, 2)
    except Exception as exc:
        print(f"[WARN] speed test unavailable: {type(exc).__name__}", flush=True)
        return None


def get_trigger_inputs() -> tuple[str, str]:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        return "auto", ""
    try:
        with open(event_path, encoding="utf-8") as file:
            inputs = json.load(file).get("inputs", {})
        return str(inputs.get("slot") or "auto"), str(inputs.get("handoff_from") or "")
    except (OSError, json.JSONDecodeError):
        return "auto", ""


def reserve_slot(repo: str, token: str, requested: str, handoff_from: str) -> tuple[int, dict]:
    current = run_id()
    active = list_active_runs(repo, token)
    chosen: dict[str, int] = {}

    def mutate(state: dict):
        slots = state.setdefault("slots", {})
        # Expired/stopped Actions must not permanently consume a pool slot.
        stale = []
        stale_entries = []
        for key, entry in list(slots.items()):
            owner = str(entry.get("run_id", ""))
            pending = str(entry.get("pending", {}).get("run_id", ""))
            heartbeat = parse_time(entry.get("updated_at"))
            owner_live = owner in active
            pending_live = pending in active if pending else False
            if owner and not owner_live and not pending_live and heartbeat and (datetime.now(timezone.utc) - heartbeat).total_seconds() > LEASE_SECONDS:
                stale.append(key)
                stale_entries.append(entry)
                del slots[key]
        if stale:
            return True, {"cleanup_only": True, "stale_entries": stale_entries}
        if handoff_from:
            candidates = [key for key, entry in slots.items() if str(entry.get("run_id")) == handoff_from]
            if len(candidates) != 1:
                raise RuntimeError("Handoff source no longer owns a proxy slot; cancelling successor safely")
            key = candidates[0]
            existing = slots[key]
            existing["pending"] = {"run_id": current, "status": "starting", "updated_at": now_iso()}
            existing["updated_at"] = now_iso()
            chosen["slot"] = int(key)
            return True, {"slot": int(key), "baseline": existing.get("dns_baseline"), "stale": stale}
        if requested in ("1", "2", "3"):
            numbers = [int(requested)]
        else:
            numbers = [1, 2, 3]
        slot = next((n for n in numbers if slot_key(n) not in slots), None)
        if slot is None:
            raise RuntimeError("All three proxy slots are in use. Stop one Actions run and retry.")
        chosen["slot"] = slot
        slots[slot_key(slot)] = {
            "run_id": current, "status": "starting", "server": f"proxy{slot}.{ZONE}",
            "started_at": now_iso(), "updated_at": now_iso(), "dns_baseline": None,
        }
        return True, {"slot": slot, "baseline": None, "stale": stale}

    info = update_state(repo, token, f"Reserve proxy slot for run {current}", mutate)
    if info.get("cleanup_only"):
        # Restore stale leases before allowing their slots to be reused.
        for entry in info.get("stale_entries", []):
            baseline = entry.get("dns_baseline")
            host = entry.get("pinggy_host")
            server = entry.get("server", "")
            if baseline and host and server:
                try:
                    CloudflareDNS().restore_if_pinggy(server, baseline)
                except Exception as exc:
                    raise RuntimeError(f"Could not restore stale DNS for {server}: {exc}") from exc
        return reserve_slot(repo, token, requested, handoff_from)
    return info["slot"], info


def save_baseline(repo: str, token: str, slot: int, baseline: dict, handoff_from: str) -> None:
    current = run_id()
    key = slot_key(slot)

    def mutate(state: dict):
        entry = state.get("slots", {}).get(key)
        if handoff_from:
            if not entry or str(entry.get("run_id")) != handoff_from or str(entry.get("pending", {}).get("run_id")) != current:
                return False, None
            entry["pending"]["dns_baseline"] = baseline
        else:
            if not entry or str(entry.get("run_id")) != current:
                return False, None
            entry["dns_baseline"] = baseline
        entry["updated_at"] = now_iso()
        return True, None
    update_state(repo, token, f"Save DNS baseline for proxy{slot}", mutate)


def activate_slot(repo: str, token: str, slot: int, host: str, port: int, baseline: dict, handoff_from: str, speed: float | None) -> bool:
    current = run_id()
    key = slot_key(slot)

    def mutate(state: dict):
        slots = state.setdefault("slots", {})
        if handoff_from:
            old = slots.get(key)
            if not old or str(old.get("run_id")) != handoff_from or str(old.get("pending", {}).get("run_id")) != current:
                return False, False
            original = old.get("dns_baseline") or baseline
        else:
            old = slots.get(key)
            if not old or str(old.get("run_id")) != current:
                return False, False
            original = old.get("dns_baseline") or baseline
        started = now_iso()
        slots[key] = {
            "run_id": current, "status": "online", "server": f"proxy{slot}.{ZONE}", "port": port,
            "pinggy_host": host, "started_at": started, "updated_at": started,
            "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=PINGGY_LIFETIME)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "speed_mbps": speed, "connections": active_connections(), "dns_baseline": original,
        }
        return True, True
    return bool(update_state(repo, token, f"Publish proxy{slot} endpoint for run {current}", mutate))


def heartbeat(repo: str, token: str, slot: int, speed: float | None) -> bool:
    current = run_id()
    key = slot_key(slot)

    def mutate(state: dict):
        entry = state.get("slots", {}).get(key)
        if not entry or str(entry.get("run_id")) != current or entry.get("status") != "online":
            return False, False
        entry["updated_at"] = now_iso()
        entry["connections"] = active_connections()
        entry["speed_mbps"] = speed
        return True, True
    return bool(update_state(repo, token, f"Refresh proxy{slot} health", mutate))


def read_slot(repo: str, token: str, slot: int) -> dict | None:
    state, _ = read_state(repo, token)
    return state.get("slots", {}).get(slot_key(slot))


def dispatch_successor(repo: str, control_token: str, slot: int, old_run: str) -> None:
    if not control_token:
        raise RuntimeError("Set PROXY_CONTROL_TOKEN with Actions:write permission to enable automatic handoff")
    body = {"ref": "main", "inputs": {"slot": str(slot), "handoff_from": old_run}}
    workflow_file = f"proxy-slot-{slot}.yml"
    status, result = github_api(repo, control_token, f"actions/workflows/{workflow_file}/dispatches", "POST", body)
    if status != 204:
        raise RuntimeError(f"Could not start {workflow_file} (HTTP {status}): {result.get('message', result)}")
    print(f"[HANDOFF] started workflow {workflow_file} for proxy{slot}.{ZONE}", flush=True)


def dispatch_initial_pool_slot() -> int:
    """Reserve the lowest free slot, then start its dedicated worker workflow."""
    repo, token, current = github_context()
    control = os.environ.get("PROXY_CONTROL_TOKEN", "").strip()
    if not control:
        raise RuntimeError("Set PROXY_CONTROL_TOKEN with Actions:write permission")
    dns = CloudflareDNS()
    slot, reservation = reserve_slot(repo, token, "auto", "")
    baseline = reservation.get("baseline") or dns.snapshot(f"proxy{slot}.{ZONE}")
    save_baseline(repo, token, slot, baseline, "")
    print(f"[ALLOCATOR] reserved the lowest available slot proxy{slot}.{ZONE}", flush=True)
    dispatch_successor(repo, control, slot, current)

    deadline = time.monotonic() + HANDOFF_WAIT
    while not STOP.is_set() and time.monotonic() < deadline:
        entry = read_slot(repo, token, slot)
        if entry and str(entry.get("run_id")) != current and entry.get("status") == "online":
            print(f"[ALLOCATOR] dedicated slot workflow published proxy{slot}.{ZONE}", flush=True)
            return 0
        STOP.wait(2)
    if STOP.is_set():
        return 130
    raise RuntimeError(f"Dedicated slot workflow did not publish proxy{slot}.{ZONE} before timeout")


def upstream_cancelled(repo: str, token: str, handoff_from: str) -> bool:
    if not handoff_from:
        return False
    status, run = github_api(repo, token, f"actions/runs/{handoff_from}")
    return status == 200 and run.get("status") == "completed" and run.get("conclusion") == "cancelled"


def remove_pending(repo: str, token: str, slot: int, handoff_from: str) -> None:
    current = run_id()
    key = slot_key(slot)
    def mutate(state: dict):
        entry = state.get("slots", {}).get(key)
        if entry and str(entry.get("run_id")) == handoff_from and str(entry.get("pending", {}).get("run_id")) == current:
            entry.pop("pending", None)
            entry["updated_at"] = now_iso()
            return True, None
        return False, None
    update_state(repo, token, f"Withdraw pending proxy{slot} handoff", mutate)


def cleanup_slot(repo: str, token: str, slot: int, tunnel: Tunnel | None, dns: CloudflareDNS | None, baseline: dict | None, handoff_from: str, published: bool) -> None:
    current = run_id()
    key = slot_key(slot)
    owned: dict = {"value": False}
    def mutate(state: dict):
        entry = state.get("slots", {}).get(key)
        if entry and str(entry.get("run_id")) == current:
            owned["value"] = True
            baseline_for_dns = entry.get("dns_baseline") or baseline
            host = entry.get("pinggy_host") or (tunnel.host if tunnel else None)
            del state["slots"][key]
            return True, {"baseline": baseline_for_dns, "host": host}
        if entry and handoff_from and str(entry.get("run_id")) == handoff_from and str(entry.get("pending", {}).get("run_id")) == current:
            entry.pop("pending", None)
            entry["updated_at"] = now_iso()
            return True, None
        return False, None
    result = update_state(repo, token, f"Release proxy{slot} for run {current}", mutate)
    if owned["value"] and isinstance(result, dict) and result.get("baseline") and result.get("host") and dns:
        try:
            dns.restore_if_owned(f"proxy{slot}.{ZONE}", result["host"], result["baseline"])
        except Exception as exc:
            print(f"::error::DNS cleanup failed: {exc}", flush=True)
    if tunnel:
        tunnel.close()


def cleanup_only() -> int:
    """Idempotent post-job cleanup; runs as a separate always() workflow step."""
    repo, token, current = github_context()
    state, _ = read_state(repo, token)
    selected = None
    for key, entry in state.get("slots", {}).items():
        if str(entry.get("run_id")) == current or str(entry.get("pending", {}).get("run_id")) == current:
            selected = (int(key), entry)
            break
    if not selected:
        print("[CLEANUP] this run owns no proxy slot; nothing to do", flush=True)
        return 0

    slot, entry = selected
    owns_slot = str(entry.get("run_id")) == current
    baseline = entry.get("dns_baseline") or entry.get("pending", {}).get("dns_baseline")
    child = str(entry.get("pending", {}).get("run_id", "")) if str(entry.get("run_id")) == current else ""
    control = os.environ.get("PROXY_CONTROL_TOKEN", "").strip()
    if child and child != current and control:
        status, result = github_api(repo, control, f"actions/runs/{child}/cancel", "POST")
        if status not in (202, 409, 404):
            print(f"::warning::could not cancel pending successor (HTTP {status}): {result.get('message')}", flush=True)
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            status, run = github_api(repo, token, f"actions/runs/{child}")
            if status == 200 and run.get("status") == "completed":
                break
            time.sleep(2)

    key = slot_key(slot)
    removed: dict = {"entry": None}
    def mutate(latest: dict):
        value = latest.get("slots", {}).get(key)
        if value and str(value.get("run_id")) == current:
            removed["entry"] = value
            del latest["slots"][key]
            return True, None
        if value and str(value.get("pending", {}).get("run_id")) == current:
            value.pop("pending", None)
            value["updated_at"] = now_iso()
            removed["entry"] = value
            return True, None
        return False, None
    update_state(repo, token, f"Cancel-safe cleanup for proxy{slot}", mutate)
    if owns_slot and baseline:
        try:
            CloudflareDNS().restore_if_pinggy(f"proxy{slot}.{ZONE}", baseline)
        except Exception as exc:
            print(f"::error::Cancel-safe DNS cleanup failed: {exc}", flush=True)
            return 1
    print(f"[CLEANUP] proxy{slot} state and DNS cleanup completed", flush=True)
    return 0


def acquire_local_proxy() -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, str(ROOT / "proxy.py")], cwd=ROOT,
        env={**os.environ, "PROXY_PORT": "8080"}, stdin=subprocess.DEVNULL, start_new_session=True)
    CHILD_GROUPS.add(proc.pid)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not STOP.is_set():
        if proc.poll() is not None:
            raise RuntimeError(f"proxy.py exited during startup ({proc.returncode})")
        try:
            with socket.create_connection(("127.0.0.1", 8080), timeout=1):
                return proc
        except OSError:
            time.sleep(0.25)
    raise RuntimeError("proxy.py did not start listening on port 8080")


def stop_proxy(proc: subprocess.Popen | None) -> None:
    if not proc:
        return
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=3)
    CHILD_GROUPS.discard(proc.pid)


def main() -> int:
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGHUP, signal_handler)
    if len(sys.argv) > 1 and sys.argv[1] == "--cleanup-only":
        return cleanup_only()
    if len(sys.argv) > 1 and sys.argv[1] == "--dispatch-auto":
        return dispatch_initial_pool_slot()
    repo, token, current = github_context()
    requested, handoff_from = get_trigger_inputs()
    requested = os.environ.get("PROXY_POOL_SLOT", "").strip() or requested
    handoff_from = os.environ.get("PROXY_POOL_HANDOFF_FROM", handoff_from).strip()
    if handoff_from and upstream_cancelled(repo, token, handoff_from):
        print("[STOP] parent workflow was cancelled before successor startup", flush=True)
        return 0
    for key in ("PROXY_USER", "PROXY_PASS", "CF_API_TOKEN", "CF_ZONE_ID"):
        if not os.environ.get(key, "").strip():
            raise RuntimeError(f"Required Actions secret is missing: {key}")
    if requested not in ("auto", "1", "2", "3"):
        raise RuntimeError("slot input must be auto, 1, 2, or 3")

    dns = CloudflareDNS()
    slot, reservation = reserve_slot(repo, token, requested, handoff_from)
    baseline = reservation.get("baseline")
    if handoff_from and baseline is None:
        old = read_slot(repo, token, slot)
        baseline = (old or {}).get("dns_baseline")
    if not baseline:
        baseline = dns.snapshot(f"proxy{slot}.{ZONE}")
    save_baseline(repo, token, slot, baseline, handoff_from)
    print(f"[SLOT] reserved proxy{slot}.{ZONE} for run {current}", flush=True)

    proxy: subprocess.Popen | None = None
    tunnel: Tunnel | None = None
    published = False
    handed_off = False
    exit_code = 0
    active_started: float | None = None
    last_heartbeat = 0.0
    handoff_deadline: float | None = None
    successor_dispatched = False
    try:
        proxy = acquire_local_proxy()
        tunnel = Tunnel()
        tunnel.wait_endpoint()
        assert tunnel.host and tunnel.port
        hostname = f"proxy{slot}.{ZONE}"
        dns.point(hostname, tunnel.host, baseline)
        initial_speed = speed_test()
        if handoff_from and upstream_cancelled(repo, token, handoff_from):
            dns.restore_if_owned(hostname, tunnel.host, baseline)
            raise RuntimeError("Parent workflow was cancelled during successor startup")
        if not activate_slot(repo, token, slot, tunnel.host, tunnel.port, baseline, handoff_from, initial_speed):
            dns.restore_if_owned(hostname, tunnel.host, baseline)
            raise RuntimeError("Slot reservation was cancelled or replaced before publication")
        published = True
        active_started = time.monotonic()
        last_heartbeat = active_started
        print(f"::notice title=Proxy pool online::proxy{slot}.rokko-st.com:{tunnel.port} (Pinggy endpoint {tunnel.host})", flush=True)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as out:
                out.write(f"## Proxy pool: proxy{slot}.rokko-st.com\n\n")
                out.write(f"- Port: `{tunnel.port}` (Pinggy Free assigned port)\n- Host: `{tunnel.host}`\n- Speed test: `{initial_speed if initial_speed is not None else 'unavailable'}` Mbps\n")

        while not STOP.is_set():
            entry = read_slot(repo, token, slot)
            if entry and str(entry.get("run_id")) != current:
                # The successor has published the new endpoint to the list. Give DNS caches time to drain.
                if entry.get("status") == "online":
                    print("[HANDOFF] successor is online and the public proxy list is updated", flush=True)
                    wait_until = time.monotonic() + TTL + 5
                    while time.monotonic() < wait_until and not STOP.is_set():
                        STOP.wait(1)
                    handed_off = True
                    break
            if handoff_from and upstream_cancelled(repo, token, handoff_from):
                raise RuntimeError("Parent workflow was cancelled; successor is stopping")
            now = time.monotonic()
            elapsed = now - active_started
            if not successor_dispatched and (tunnel.proc.poll() is not None or elapsed >= HANDOFF_AFTER):
                if tunnel.proc.poll() is not None:
                    print("[HANDOFF] Pinggy tunnel disconnected; starting a successor", flush=True)
                else:
                    print("[HANDOFF] starting successor before the 60-minute Pinggy Free expiry", flush=True)
                dispatch_successor(repo, os.environ.get("PROXY_CONTROL_TOKEN", "").strip(), slot, current)
                handoff_deadline = now + HANDOFF_WAIT
                successor_dispatched = True
            if handoff_deadline and time.monotonic() > handoff_deadline:
                raise RuntimeError("Successor did not publish itself to the proxy list before timeout")
            if not successor_dispatched and now - last_heartbeat >= HEARTBEAT:
                measured = speed_test()
                if not heartbeat(repo, token, slot, measured):
                    entry = read_slot(repo, token, slot)
                    if entry and str(entry.get("run_id")) != current:
                        continue
                    raise RuntimeError("Could not refresh this run's proxy-list entry")
                print(f"[HEARTBEAT] proxy{slot}: speed={measured} Mbps, connections={active_connections()}", flush=True)
                last_heartbeat = time.monotonic()
            STOP.wait(2)
    except Exception as exc:
        if STOP.is_set():
            print(f"[STOP] cancelling: {exc}", flush=True)
        else:
            print(f"::error::{exc}", flush=True)
            exit_code = 1
    finally:
        if handoff_from and not published:
            if tunnel and tunnel.host and baseline:
                try:
                    dns.restore_if_owned(f"proxy{slot}.{ZONE}", tunnel.host, baseline)
                except Exception as exc:
                    print(f"::error::successor DNS rollback failed: {exc}", flush=True)
            try:
                remove_pending(repo, token, slot, handoff_from)
            except Exception as exc:
                print(f"::warning::Could not clear pending reservation: {exc}", flush=True)
        try:
            cleanup_slot(repo, token, slot, tunnel, dns, baseline, handoff_from, published)
        except Exception as exc:
            print(f"::error::cleanup failed: {exc}", flush=True)
            exit_code = 1
        stop_proxy(proxy)
        print("[STOP] Pinggy SSH process group and proxy.py stopped", flush=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
