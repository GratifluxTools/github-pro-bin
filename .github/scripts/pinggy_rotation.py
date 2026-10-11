#!/usr/bin/env python3
"""Run a local HTTP proxy behind rotating free Pinggy TCP tunnels."""
from __future__ import annotations

import argparse
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
DNS_NAME = "proxy.rokko-st.com"
DNS_TTL_SECONDS = 60
PINGGY_ENDPOINT = re.compile(
    r"(?P<host>[A-Za-z0-9.-]+\.(?:pinggy\.io|pinggy\.link)):(?P<port>[0-9]{2,5})"
)
STOP = threading.Event()
CHILD_GROUPS: set[int] = set()


def register_child(process: subprocess.Popen) -> None:
    CHILD_GROUPS.add(process.pid)


def unregister_child(process: subprocess.Popen) -> None:
    CHILD_GROUPS.discard(process.pid)


def handle_signal(signum: int, _frame: object) -> None:
    print(f"[STOP] received signal {signum}; stopping children", flush=True)
    STOP.set()
    for process_group in tuple(CHILD_GROUPS):
        try:
            os.killpg(process_group, signal.SIGTERM)
        except ProcessLookupError:
            pass


def cf_request(method: str, path: str, payload: dict | None = None) -> dict:
    token = os.environ.get("CF_API_TOKEN", "")
    zone_id = os.environ.get("CF_ZONE_ID", "")
    if not token or not zone_id:
        raise RuntimeError("Set CF_API_TOKEN and CF_ZONE_ID Actions secrets (DNS Read + DNS Write)")

    url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "github-pro-bin-pinggy-rotation",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        # The response is from Cloudflare and does not contain the bearer token.
        raise RuntimeError(f"Cloudflare DNS API returned HTTP {exc.code}: {body[:1000]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Cloudflare DNS API connection failed: {exc.reason}") from exc
    if not result.get("success"):
        errors = result.get("errors", [])
        raise RuntimeError(f"Cloudflare DNS API rejected the request: {errors}")
    return result


class DNSManager:
    """Update the Pinggy alias during service and restore its original record on exit."""

    def __init__(self):
        query = urllib.parse.urlencode({"name": DNS_NAME, "per_page": 100})
        data = cf_request("GET", f"/dns_records?{query}")
        records = data.get("result", [])
        self.exact = [r for r in records if r.get("name", "").rstrip(".").lower() == DNS_NAME]
        self.record_id: str | None = None
        self.original: dict | None = None
        self.created = False
        self.original_deleted = False
        if self.exact:
            if len(self.exact) != 1 or self.exact[0].get("type") not in ("A", "CNAME"):
                kinds = ", ".join(f"{r.get('type')}:{r.get('id')}" for r in self.exact)
                raise RuntimeError(
                    f"Cannot safely update {DNS_NAME}: expected exactly one A or CNAME record, found {kinds}. "
                    "Resolve conflicting A/AAAA/CNAME records manually first."
                )
            record = self.exact[0]
            if record.get("type") == "CNAME":
                self.record_id = record["id"]
            self.original = {
                key: record[key]
                for key in ("type", "name", "content", "ttl", "proxied", "comment", "tags", "settings")
                if key in record
            }

    def update(self, host: str) -> None:
        """Point the DNS-only CNAME at the currently active Pinggy host."""
        payload = {
            "type": "CNAME",
            "name": DNS_NAME,
            "content": host,
            "ttl": DNS_TTL_SECONDS,
            "proxied": False,
            "comment": "Managed by github-pro-bin Pinggy rotation workflow",
        }
        if self.record_id:
            cf_request("PATCH", f"/dns_records/{self.record_id}", payload)
        else:
            if self.original and self.original["type"] == "A" and not self.original_deleted:
                cf_request("DELETE", f"/dns_records/{self.exact[0]['id']}")
                self.original_deleted = True
            created = cf_request("POST", "/dns_records", payload).get("result", {})
            self.record_id = created.get("id")
            self.created = True

        print(
            f"[DNS] {DNS_NAME} CNAME -> {host} (DNS-only, TTL={DNS_TTL_SECONDS}s). "
            "DNS does not carry the TCP port; configure the port shown below.",
            flush=True,
        )

    def restore(self) -> None:
        if self.created and self.record_id:
            cf_request("DELETE", f"/dns_records/{self.record_id}")
            self.record_id = None
        if self.original and self.original_deleted:
            restored = cf_request("POST", "/dns_records", self.original).get("result", {})
            self.record_id = restored.get("id")
            self.original_deleted = False
            print(f"[DNS] Restored original {self.original['type']} record for {DNS_NAME}", flush=True)
        elif self.original and self.record_id:
            cf_request("PATCH", f"/dns_records/{self.record_id}", self.original)
            print(f"[DNS] Restored original record for {DNS_NAME}", flush=True)
        elif self.created:
            print(f"[DNS] Removed temporary CNAME {DNS_NAME}", flush=True)


def wait_dns_grace(tunnel: "Tunnel", seconds: int = DNS_TTL_SECONDS + 10) -> None:
    """Let resolvers holding the previous DNS answer expire before dropping it."""
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if STOP.is_set():
            return
        if tunnel.process.poll() is not None:
            raise RuntimeError(f"New Pinggy tunnel #{tunnel.number} disconnected during DNS propagation")
        STOP.wait(min(1, until - time.monotonic()))


def summary_line(text: str) -> None:
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as out:
            out.write(text + "\n")


class Tunnel:
    def __init__(self, number: int, log_dir: Path):
        self.number = number
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.log_path = log_dir / f"pinggy-{number}.log"
        self.log = self.log_path.open("w", encoding="utf-8", buffering=1)
        self.process = subprocess.Popen(
            [
                "ssh", "-T",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ExitOnForwardFailure=yes",
                "-o", "ServerAliveInterval=20",
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
        register_child(self.process)
        self.reader = threading.Thread(target=self._read_output, daemon=True)
        self.reader.start()
        self.endpoint: str | None = None
        self.host: str | None = None
        self.port: str | None = None

    def _read_output(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                self.log.write(line)
                self.lines.put(line.rstrip("\n"))
        finally:
            self.lines.put(None)

    def wait_until_ready(self, timeout: int = 90) -> str:
        deadline = time.monotonic() + timeout
        while not STOP.is_set() and time.monotonic() < deadline:
            if self.process.poll() is not None:
                break
            try:
                line = self.lines.get(timeout=0.5)
            except queue.Empty:
                continue
            if line is None:
                break
            print(f"[Pinggy #{self.number}] {line}", flush=True)
            match = PINGGY_ENDPOINT.search(line)
            if match:
                self.host = match.group("host")
                self.port = match.group("port")
                self.endpoint = f"{self.host}:{self.port}"
                return self.endpoint

        self.stop()
        try:
            tail = "\n".join(self.log_path.read_text(encoding="utf-8").splitlines()[-30:])
        except OSError:
            tail = ""
        raise RuntimeError(
            f"Pinggy #{self.number} did not report a public TCP endpoint within {timeout}s. "
            f"SSH exit={self.process.poll()}; log={self.log_path}\n{tail}"
        )

    def stop(self) -> None:
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.process.wait(timeout=3)
        self.reader.join(timeout=1)
        if not self.log.closed:
            self.log.close()
        unregister_child(self.process)


def stop_all(tunnels: list[Tunnel], proxy: subprocess.Popen[bytes] | None) -> None:
    for tunnel in reversed(tunnels):
        tunnel.stop()
    if proxy is not None and proxy.poll() is None:
        try:
            os.killpg(proxy.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proxy.wait(timeout=8)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proxy.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proxy.wait(timeout=3)
    if proxy is not None:
        unregister_child(proxy)


def check_proxy_ready() -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not STOP.is_set():
        try:
            with socket.create_connection(("127.0.0.1", 8080), timeout=1):
                return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError("Local proxy did not listen on 127.0.0.1:8080")


def emit_endpoint(tunnel: Tunnel) -> None:
    assert tunnel.endpoint
    text = (
        f"Client proxy address: {DNS_NAME}:{tunnel.port} "
        f"(DNS CNAME currently points to {tunnel.host}; TCP port is assigned by free Pinggy and changes on rotation)."
    )
    print(f"::notice title=Pinggy接続先::{text}", flush=True)
    print(f"[ENDPOINT] {text}", flush=True)
    summary_line(f"- Tunnel #{tunnel.number}: `{DNS_NAME}:{tunnel.port}` (CNAME target `{tunnel.host}`)")


def run(args: argparse.Namespace) -> int:
    if not os.environ.get("PROXY_USER") or not os.environ.get("PROXY_PASS"):
        raise RuntimeError("Set both PROXY_USER and PROXY_PASS in repository Actions secrets")

    logs = ROOT / "pinggy-logs"
    logs.mkdir(exist_ok=True)
    tunnels: list[Tunnel] = []
    proxy: subprocess.Popen[bytes] | None = None
    dns: DNSManager | None = None
    exit_code = 0

    try:
        proxy = subprocess.Popen(
            [sys.executable, str(ROOT / "proxy.py")],
            cwd=ROOT,
            env={**os.environ, "PROXY_PORT": "8080"},
            stdin=subprocess.DEVNULL,
            stdout=None,
            stderr=None,
            start_new_session=True,
        )
        register_child(proxy)
        check_proxy_ready()
        started_at = time.monotonic()
        deadline = started_at + args.runtime_seconds
        number = 1
        current = Tunnel(number, logs)
        tunnels.append(current)
        current.wait_until_ready()
        dns = DNSManager()
        dns.update(current.host or "")
        emit_endpoint(current)

        if not args.auto_switch:
            print("[INFO] Automatic switch is disabled; ending when this free tunnel disconnects or runtime expires.", flush=True)
            while not STOP.is_set() and time.monotonic() < deadline:
                if current.process.poll() is not None:
                    print("[INFO] Current Pinggy tunnel disconnected; automatic switch is disabled.", flush=True)
                    break
                if proxy.poll() is not None:
                    raise RuntimeError(f"proxy.py exited unexpectedly: {proxy.returncode}")
                STOP.wait(min(1, max(0, deadline - time.monotonic())))
            return exit_code

        print(
            f"[INFO] Auto-switch enabled: every {args.rotation_seconds // 60} minutes; "
            f"maximum runtime {args.runtime_seconds // 60} minutes.",
            flush=True,
        )
        next_rotation = time.monotonic() + args.rotation_seconds

        while not STOP.is_set() and time.monotonic() < deadline:
            if proxy.poll() is not None:
                raise RuntimeError(f"proxy.py exited unexpectedly: {proxy.returncode}")
            if current.process.poll() is not None:
                print("[WARN] Current tunnel ended early; starting its replacement immediately.", flush=True)
                next_rotation = time.monotonic()

            remaining = min(next_rotation, deadline) - time.monotonic()
            if remaining > 0:
                STOP.wait(min(1, remaining))
                continue
            if STOP.is_set() or time.monotonic() >= deadline:
                break

            number += 1
            print(f"[INFO] Starting replacement Pinggy tunnel #{number} before stopping #{current.number}.", flush=True)
            replacement = Tunnel(number, logs)
            tunnels.append(replacement)
            replacement.wait_until_ready()
            try:
                assert dns is not None
                dns.update(replacement.host or "")
                emit_endpoint(replacement)
                wait_dns_grace(replacement)
            except Exception:
                # Roll DNS back while the old tunnel is still alive if its endpoint remains valid.
                if current.process.poll() is None and current.host:
                    try:
                        assert dns is not None
                        dns.update(current.host)
                    except Exception as rollback_error:
                        print(f"::error::DNS rollback failed: {rollback_error}", flush=True)
                raise

            current.stop()
            tunnels.remove(current)
            current = replacement
            next_rotation = time.monotonic() + args.rotation_seconds

        if time.monotonic() >= deadline:
            print("[INFO] Maximum runtime reached; stopping tunnel(s) and proxy.", flush=True)
        return exit_code

    except Exception as exc:
        print(f"::error::{exc}", flush=True)
        return 1
    finally:
        if dns is not None:
            try:
                dns.restore()
            except Exception as restore_error:
                print(f"::error::Could not restore original DNS record: {restore_error}", flush=True)
        stop_all(tunnels, proxy)
        print("[STOP] all Pinggy SSH process groups and proxy.py stopped", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--auto-switch", choices=("true", "false"), default="true")
    parser.add_argument("--runtime-seconds", type=int, default=5 * 60 * 60 + 55 * 60)
    parser.add_argument("--rotation-seconds", type=int, default=55 * 60)
    args = parser.parse_args()
    args.auto_switch = args.auto_switch == "true"

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGHUP, handle_signal)

    return run(args)


if __name__ == "__main__":
    sys.exit(main())
