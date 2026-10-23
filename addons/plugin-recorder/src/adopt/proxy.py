#!/usr/bin/env python3
"""
Adopt proxy: the bridge between an existing recorder stack and this plugin.

Why it exists
-------------
On a server where the reference recorder already runs, ``recording-controller``
and ``rec-http`` are bound to the host network and listen on 127.0.0.1 only.
Traefik lives in its own docker network and cannot reach 127.0.0.1 of the host.
Mounting the existing containers into Traefik's network is not an option: that
would mean editing the running stack's compose file, which this plugin
promises never to do.

So instead a small proxy joins the Traefik network, is given the host gateway
through ``extra_hosts``, and forwards two prefixes:

    :8765  ->  UPSTREAM_API     (the existing recording controller)
    :8899  ->  UPSTREAM_PORTAL  (the existing recordings archive)

Two ports, two Traefik services, two routers -- exactly like the standalone
overlay. The existing stack keeps its own ports and is never restarted.
"""

import os
import socket
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UP_API = os.environ.get("UPSTREAM_API", "http://host.docker.internal:8765").rstrip("/")
UP_PORTAL = os.environ.get("UPSTREAM_PORTAL", "http://host.docker.internal:8899").rstrip("/")
TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "420"))

# Headers that describe a single hop and must not be forwarded twice; a wrong
# Content-Length or hop-by-hop header breaks the response Traefik receives.
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}


def log(msg):
    sys.stdout.write("[adopt] %s\n" % msg)
    sys.stdout.flush()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream = ""            # set by the subclass factory
    server_version = "recorder-plugin-adopt"
    sys_version = ""

    def log_message(self, fmt, *args):
        log("%s %s" % (self.address_string(), fmt % args))

    def _proxy(self, method):
        url = self.upstream + self.path
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None

        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                declared = resp.headers.get("Content-Length")
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() not in HOP:
                        self.send_header(k, v)
                if declared is None:
                    # no length upstream: delimit by closing the connection
                    self.close_connection = True
                    self.send_header("Connection", "close")
                else:
                    # keep the length so Traefik can report progress and the
                    # browser can seek; the body is still streamed
                    self.send_header("Content-Length", declared)
                self.end_headers()
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except urllib.error.HTTPError as exc:
            payload = exc.read() or b""
            self.send_response(exc.code)
            self.send_header("Content-Type", exc.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            payload = ('{"ok":false,"error":"upstream unreachable: %s"}' % exc).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    def do_GET(self):
        if self.path.split("?")[0] in ("/health", "/healthz"):
            payload = b'{"ok":true,"mode":"adopt","upstream":"%s"}' % self.upstream.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self._proxy("GET")

    def do_HEAD(self):
        self._proxy("HEAD")

    def do_POST(self):
        self._proxy("POST")

    def do_PUT(self):
        self._proxy("PUT")

    def do_DELETE(self):
        self._proxy("DELETE")

    def do_OPTIONS(self):
        self._proxy("OPTIONS")


def make(upstream, port):
    cls = type("Handler%d" % port, (Handler,), {"upstream": upstream})
    return ThreadingHTTPServer(("0.0.0.0", port), cls)


def main():
    servers = [make(UP_API, 8765), make(UP_PORTAL, 8899)]
    log("API    :8765 -> %s" % UP_API)
    log("portal :8899 -> %s" % UP_PORTAL)
    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        log("stopped")


if __name__ == "__main__":
    main()
