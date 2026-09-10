#!/usr/bin/env python3

import base64
import os
import select
import socket
import threading
import urllib.parse


HOST = "127.0.0.1"
PORT = int(os.environ.get("PROXY_PORT", "8080"))

PROXY_USER = os.environ.get("PROXY_USER", "")
PROXY_PASS = os.environ.get("PROXY_PASS", "")

BUFFER_SIZE = 64 * 1024
TIMEOUT = 30


def send_error(client, code, message):
    body = f"{code} {message}\r\n".encode()

    response = (
        f"HTTP/1.1 {code} {message}\r\n"
        f"Content-Type: text/plain\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode() + body

    try:
        client.sendall(response)
    except Exception:
        pass


def check_auth(headers):
    if not PROXY_USER and not PROXY_PASS:
        return True

    value = headers.get("proxy-authorization")

    if not value:
        return False

    if not value.lower().startswith("basic "):
        return False

    try:
        decoded = base64.b64decode(
            value[6:].strip()
        ).decode("utf-8")

        username, password = decoded.split(":", 1)

        return (
            username == PROXY_USER
            and password == PROXY_PASS
        )

    except Exception:
        return False


def parse_headers(data):
    text = data.decode("iso-8859-1")

    lines = text.split("\r\n")

    request_line = lines[0]

    headers = {}

    for line in lines[1:]:
        if not line:
            break

        if ":" not in line:
            continue

        key, value = line.split(":", 1)

        headers[key.strip().lower()] = value.strip()

    return request_line, headers


def relay(client, remote):
    sockets = [client, remote]

    try:
        while True:
            readable, _, exceptional = select.select(
                sockets,
                [],
                sockets,
                TIMEOUT
            )

            if exceptional:
                break

            if not readable:
                break

            for sock in readable:
                data = sock.recv(BUFFER_SIZE)

                if not data:
                    return

                destination = (
                    remote
                    if sock is client
                    else client
                )

                destination.sendall(data)

    except Exception:
        pass

    finally:
        try:
            client.close()
        except Exception:
            pass

        try:
            remote.close()
        except Exception:
            pass


def handle_client(client, address):
    client.settimeout(TIMEOUT)

    try:
        data = b""

        while b"\r\n\r\n" not in data:
            chunk = client.recv(BUFFER_SIZE)

            if not chunk:
                return

            data += chunk

            if len(data) > 1024 * 1024:
                send_error(
                    client,
                    431,
                    "Request Header Fields Too Large"
                )
                return

        header_end = data.find(b"\r\n\r\n")

        header_data = data[:header_end + 4]
        remaining = data[header_end + 4:]

        request_line, headers = parse_headers(header_data)

        if not check_auth(headers):
            response = (
                "HTTP/1.1 407 Proxy Authentication Required\r\n"
                "Proxy-Authenticate: Basic realm=\"Proxy\"\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode()

            client.sendall(response)
            return

        parts = request_line.split()

        if len(parts) < 2:
            send_error(client, 400, "Bad Request")
            return

        method = parts[0]
        target = parts[1]

        # HTTPS
        if method.upper() == "CONNECT":
            if ":" in target:
                host, port_text = target.rsplit(":", 1)

                try:
                    port = int(port_text)
                except ValueError:
                    send_error(client, 400, "Bad Port")
                    return
            else:
                host = target
                port = 443

            remote = socket.create_connection(
                (host, port),
                timeout=TIMEOUT
            )

            client.sendall(
                b"HTTP/1.1 200 Connection Established\r\n"
                b"Proxy-Agent: GratifluxProxy\r\n"
                b"\r\n"
            )

            # CONNECT後にすでに受信していたデータがあれば転送
            if remaining:
                remote.sendall(remaining)

            relay(client, remote)

            return

        # 通常のHTTP
        parsed = urllib.parse.urlsplit(target)

        if parsed.scheme and parsed.hostname:
            host = parsed.hostname
            port = parsed.port or (
                443 if parsed.scheme == "https" else 80
            )

            path = urllib.parse.urlunsplit(
                (
                    "",
                    "",
                    parsed.path or "/",
                    parsed.query,
                    ""
                )
            )
        else:
            host_header = headers.get("host")

            if not host_header:
                send_error(client, 400, "Missing Host")
                return

            if ":" in host_header:
                host, port_text = host_header.rsplit(":", 1)

                try:
                    port = int(port_text)
                except ValueError:
                    port = 80
            else:
                host = host_header
                port = 80

            path = target

        remote = socket.create_connection(
            (host, port),
            timeout=TIMEOUT
        )

        # Proxy専用ヘッダーを削除
        outgoing_headers = []

        for line in header_data.decode(
            "iso-8859-1"
        ).split("\r\n")[1:]:

            if not line:
                continue

            lower = line.lower()

            if lower.startswith("proxy-"):
                continue

            if lower.startswith("connection:"):
                continue

            outgoing_headers.append(line)

        outgoing = (
            f"{method} {path} HTTP/1.1\r\n"
            + "\r\n".join(outgoing_headers)
            + "\r\n"
            + "\r\n"
        ).encode("iso-8859-1")

        remote.sendall(outgoing)

        if remaining:
            remote.sendall(remaining)

        relay(client, remote)

    except Exception as e:
        try:
            send_error(client, 502, "Bad Gateway")
        except Exception:
            pass

    finally:
        try:
            client.close()
        except Exception:
            pass


def main():
    server = socket.socket(
        socket.AF_INET,
        socket.SOCK_STREAM
    )

    server.setsockopt(
        socket.SOL_SOCKET,
        socket.SO_REUSEADDR,
        1
    )

    server.bind((HOST, PORT))
    server.listen(100)

    print("=" * 50)
    print("Gratiflux Proxy")
    print("=" * 50)
    print(f"Listening: {HOST}:{PORT}")
    print(
        "Authentication:",
        "ON" if PROXY_USER else "OFF"
    )
    print("=" * 50)

    while True:
        client, address = server.accept()

        thread = threading.Thread(
            target=handle_client,
            args=(client, address),
            daemon=True
        )

        thread.start()


if __name__ == "__main__":
    main()
