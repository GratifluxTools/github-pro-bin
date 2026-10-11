#!/usr/bin/env python3
"""Run the existing proxy.py through Pinggy Free and temporarily alias its DNS name."""
from __future__ import annotations

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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ZONE_NAME = "rokko-st.com"
DNS_NAME = "proxy.rokko-st.com"
DNS_TTL = 60
# Pinggy Free currently reports endpoints such as:
# tcp://cznis-20-119-78-149.run.pinggy-free.link:41639
ENDPOINT_RE = re.compile(r"tcp://(?P<host>[A-Za-z0-9.-]+):(?P<port>[0-9]{1,5})")
PINGGY_SUFFIXES = (".run.pinggy-free.link", ".pinggy.io", ".pinggy.link")
STOP = threading.Event()
CHILD_GROUPS: set[int] = set()


def register_child(proc: subprocess.Popen) -> None:
    CHILD_GROUPS.add(proc.pid)


def unregister_child(proc: subprocess.Popen) -> None:
    CHILD_GROUPS.discard(proc.pid)


def handle_signal(signum: int, _frame: object) -> None:
    print(f"[STOP] received signal {signum}; stopping child processes", flush=True)
    STOP.set()
    for group_id in tuple(CHILD_GROUPS):
        try:
            os.killpg(group_id, signal.SIGTERM)
        except ProcessLookupError:
            pass


def cf_request(method: str, path: str, token: str, body: dict | None = None) -> tuple[int, dict]:
    url = f"https://api.cloudflare.com/client/v4{path}"
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "github-pro-bin-http-proxy-with-dns",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"raw": raw[:1000]}
        return exc.code, parsed
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Cloudflare API connection failed: {exc.reason}") from exc


def require_api_success(status: int, result: dict, action: str) -> dict:
    if status < 200 or status >= 300 or not result.get("success"):
        errors = result.get("errors", [])
        raise RuntimeError(f"Cloudflare {action} failed (HTTP {status}): {errors}")
    return result


class DNSManager:
    """Temporarily point the single proxy DNS record at Pinggy; restore it on exit."""

    def __init__(self) -> None:
        self.token = os.environ.get("CF_API_TOKEN", "").strip()
        zone_input = os.environ.get("CF_ZONE_ID", "").strip()
        if not self.token or not zone_input:
            raise RuntimeError("Set CF_API_TOKEN and CF_ZONE_ID repository Actions secrets")
        self.zone_id = self._resolve_zone(zone_input)
        query = urllib.parse.urlencode({"name": DNS_NAME, "per_page": 100})
        response = require_api_success(
            *cf_request("GET", f"/zones/{self.zone_id}/dns_records?{query}", self.token),
            "DNS record lookup",
        )
        records = [
            record
            for record in response.get("result", [])
            if record.get("name", "").rstrip(".").lower() == DNS_NAME
        ]
        if len(records) > 1 or (records and records[0].get("type") not in ("A", "CNAME")):
            found = ", ".join(f"{r.get('type')}:{r.get('id')}" for r in records) or "unknown records"
            raise RuntimeError(
                f"Refusing to change {DNS_NAME}: expected at most one A/CNAME record, found {found}. "
                "Resolve conflicting DNS records manually."
            )

        self.original = self._record_payload(records[0]) if records else None
        self.record_id: str | None = records[0]["id"] if records and records[0]["type"] == "CNAME" else None
        if records and records[0].get("type") == "A":
            self.original_record_id = records[0]["id"]
        self.old_a_deleted = False
        self.temporary_record = False

    @staticmethod
    def _record_payload(record: dict) -> dict:
        keys = ("type", "name", "content", "ttl", "proxied", "comment", "tags", "settings")
        return {key: record[key] for key in keys if key in record}

    def _resolve_zone(self, zone_input: str) -> str:
        quoted = urllib.parse.quote(zone_input, safe="")
        status, response = cf_request("GET", f"/zones/{quoted}", self.token)
        if status == 200 and response.get("success"):
            zone = response.get("result", {})
            if zone.get("name", "").lower() == ZONE_NAME:
                return zone.get("id", zone_input)
            raise RuntimeError(f"CF_ZONE_ID belongs to {zone.get('name')!r}, expected {ZONE_NAME!r}")

        if not any(error.get("code") == 9109 for error in response.get("errors", []) if isinstance(error, dict)):
            raise RuntimeError(f"Cloudflare zone verification failed (HTTP {status}): {response.get('errors', [])}")

        # Match the previous rokko-proxy DNS script: resolve by the fixed zone name
        # when the configured identifier is invalid, but only if the token can read zones.
        query = urllib.parse.urlencode({"name": ZONE_NAME, "per_page": 50})
        status, lookup = cf_request("GET", f"/zones?{query}", self.token)
        result = require_api_success(status, lookup, "zone-name lookup")
        matches = [z for z in result.get("result", []) if z.get("name", "").lower() == ZONE_NAME]
        if len(matches) != 1 or not matches[0].get("id"):
            raise RuntimeError(f"Expected exactly one accessible Cloudflare zone named {ZONE_NAME}; found {len(matches)}")
        print(f"[WARN] resolved {ZONE_NAME} to configured accessible Zone ID", flush=True)
        return matches[0]["id"]

    def update(self, host: str) -> None:
        desired = {
            "type": "CNAME",
            "name": DNS_NAME,
            "content": host,
            "ttl": DNS_TTL,
            "proxied": False,
            "comment": "HTTP Proxy with DNS - temporary Pinggy Free endpoint",
        }
        if self.record_id:
            status, result = cf_request(
                "PUT", f"/zones/{self.zone_id}/dns_records/{self.record_id}", self.token, desired
            )
            require_api_success(status, result, "DNS CNAME update")
        else:
            if self.original and self.original.get("type") == "A" and not self.old_a_deleted:
                status, result = cf_request(
                    "DELETE", f"/zones/{self.zone_id}/dns_records/{self.original_record_id}", self.token
                )
                require_api_success(status, result, "temporary A record removal")
                self.old_a_deleted = True
            status, result = cf_request(
                "POST", f"/zones/{self.zone_id}/dns_records", self.token, desired
            )
            created = require_api_success(status, result, "temporary CNAME creation").get("result", {})
            self.record_id = created.get("id")
            self.temporary_record = True
            if not self.record_id:
                raise RuntimeError("Cloudflare created a DNS record but returned no record ID")

        print(f"[DNS] {DNS_NAME} CNAME -> {host} (DNS-only, TTL={DNS_TTL})", flush=True)

    @property
    def original_record_id(self) -> str:
        # A records are deleted only once, after snapshotting the original payload.
        # The ID is saved separately because it is not part of the create/restore payload.
        if not hasattr(self, "_original_id"):
            raise RuntimeError("Original DNS record ID is unavailable")
        return self._original_id

    @original_record_id.setter
    def original_record_id(self, value: str) -> None:
        self._original_id = value

    def restore(self) -> None:
        if self.temporary_record and self.record_id:
            status, result = cf_request(
                "DELETE", f"/zones/{self.zone_id}/dns_records/{self.record_id}", self.token
            )
            require_api_success(status, result, "temporary CNAME removal")
            self.record_id = None
        if self.old_a_deleted and self.original:
            status, result = cf_request(
                "POST", f"/zones/{self.zone_id}/dns_records", self.token, self.original
            )
            require_api_success(status, result, "original A record restore")
            self.old_a_deleted = False
            print(f"[DNS] restored original A record for {DNS_NAME}", flush=True)
        elif self.original and self.record_id and self.original.get("type") == "CNAME":
            status, result = cf_request(
                "PUT", f"/zones/{self.zone_id}/dns_records/{self.record_id}", self.token, self.original
            )
            require_api_success(status, result, "original CNAME restore")
            print(f"[DNS] restored original CNAME for {DNS_NAME}", flush=True)
        elif self.temporary_record:
            print(f"[DNS] removed temporary CNAME for {DNS_NAME}", flush=True)


class PinggyTunnel:
    def __init__(self) -> None:
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.proc = subprocess.Popen(
            [
                "ssh", "-T",
                "-o", "StrictHostKeyChecking=no",
                "-o", "ServerAliveInterval=30",
                "-o", "ServerAliveCountMax=3",
                "-o", "ConnectTimeout=20",
                "-p", "443",
                "-R", "0:127.0.0.1:8080",
                "tcp@free.pinggy.io",
            ],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        register_child(self.proc)
        self.reader = threading.Thread(target=self._read_output, daemon=True)
        self.reader.start()
        self.host: str | None = None
        self.port: str | None = None

    def _read_output(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.rstrip("\r\n")
            print(f"[Pinggy] {line}", flush=True)
            match = ENDPOINT_RE.search(line)
            if match:
                candidate = match.group("host").lower().rstrip(".")
                if candidate.endswith(PINGGY_SUFFIXES):
                    self.host = candidate
                    self.port = match.group("port")
            self.lines.put(line)
        self.lines.put(None)

    def wait_for_endpoint(self, timeout: int = 90) -> None:
        deadline = time.monotonic() + timeout
        while not STOP.is_set() and time.monotonic() < deadline:
            if self.proc.poll() is not None:
                break
            try:
                self.lines.get(timeout=0.5)
            except queue.Empty:
                continue
            if self.host and self.port:
                return
        raise RuntimeError(
            f"Pinggy did not provide a recognized TCP endpoint within {timeout}s "
            f"(ssh exit={self.proc.poll()}). Expected domains include .run.pinggy-free.link."
        )

    def stop(self) -> None:
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.proc.wait(timeout=3)
        self.reader.join(timeout=1)
        unregister_child(self.proc)


def check_proxy_ready() -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not STOP.is_set():
        try:
            with socket.create_connection(("127.0.0.1", 8080), timeout=1):
                return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError("proxy.py did not listen on 127.0.0.1:8080")


def stop_proxy(proc: subprocess.Popen | None) -> None:
    if proc is None:
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
    unregister_child(proc)


def publish_endpoint(tunnel: PinggyTunnel) -> None:
    message = (
        f"Proxy address: {DNS_NAME}:{tunnel.port}; "
        f"DNS CNAME target: {tunnel.host}. Pinggy Free assigns the TCP port dynamically."
    )
    print(f"::notice title=HTTP Proxy with DNS::{message}", flush=True)
    print(f"[ENDPOINT] {message}", flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as output:
            output.write("## HTTP Proxy with DNS\n\n")
            output.write(f"- Client proxy address: `{DNS_NAME}:{tunnel.port}`\n")
            output.write(f"- DNS-only CNAME target: `{tunnel.host}`\n")
            output.write("- Pinggy Free TCP port is assigned dynamically; it is not port 8080.\n")


def run() -> int:
    proxy_user = os.environ.get("PROXY_USER", "")
    proxy_pass = os.environ.get("PROXY_PASS", "")
    if not proxy_user or not proxy_pass:
        raise RuntimeError("Set both PROXY_USER and PROXY_PASS repository Actions secrets")

    proxy: subprocess.Popen | None = None
    tunnel: PinggyTunnel | None = None
    dns: DNSManager | None = None
    result = 0
    try:
        proxy = subprocess.Popen(
            [sys.executable, str(ROOT / "proxy.py")],
            cwd=ROOT,
            env={**os.environ, "PROXY_PORT": "8080"},
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        register_child(proxy)
        check_proxy_ready()

        tunnel = PinggyTunnel()
        tunnel.wait_for_endpoint()
        assert tunnel.host and tunnel.port
        print(f"[OK] received Pinggy endpoint {tunnel.host}:{tunnel.port}", flush=True)

        dns = DNSManager()
        # CNAME aliases preserve only the hostname; the allocated public TCP port is output separately.
        dns.update(tunnel.host)
        publish_endpoint(tunnel)

        started = time.monotonic()
        while not STOP.is_set():
            if proxy.poll() is not None:
                raise RuntimeError(f"proxy.py exited unexpectedly (exit={proxy.returncode})")
            if tunnel.proc.poll() is not None:
                elapsed = time.monotonic() - started
                print(f"[INFO] Pinggy tunnel ended after {elapsed / 60:.1f} minutes; cleaning up.", flush=True)
                # Free Pinggy expires at one hour; a shorter-lived failed tunnel is a workflow failure.
                result = 0 if elapsed >= 10 * 60 else 1
                break
            STOP.wait(1)
    except Exception as exc:
        print(f"::error::{exc}", flush=True)
        result = 1
    finally:
        # Restore the old DNS while the local service still exists, then stop all child groups.
        if dns is not None:
            try:
                dns.restore()
            except Exception as exc:
                print(f"::error::DNS restore failed: {exc}", flush=True)
                result = 1
        if tunnel is not None:
            tunnel.stop()
        stop_proxy(proxy)
        print("[STOP] Pinggy SSH tunnel and proxy.py stopped", flush=True)
    return result


def main() -> int:
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGHUP, handle_signal)
    return run()


if __name__ == "__main__":
    sys.exit(main())
