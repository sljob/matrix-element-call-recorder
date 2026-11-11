#!/usr/bin/env python3
"""All nginx work for the recorder plugin lives here, and nowhere else.

Hard-won constraints baked into this module (each one cost a debugging session
on a live stand):

  1. A file dropped into conf.d/ is parsed in `http{}` scope. A bare
     `location` directive there is a hard config error
     ("location directive is not allowed here"). Therefore every generated
     fragment that contains `location` lives in `includes/` and is pulled in
     from a real `server{}` block by `include`. Nothing else is ever written
     into the conf.d root except a file that owns a `server{}`.

  2. The plugin never edits a file it does not own, and never edits a
     container's filesystem. Config is generated on the host, in a directory
     that is bind-mounted into the nginx container, then validated with
     `nginx -t` and only then reloaded. A failed test restores the previous
     file byte for byte.

  3. The plugin does not fight for ownership of a front proxy's `server{}`.
     The default mode is a *standalone* nginx container that owns its port and
     its entire config tree, so nothing else on the host can break it and it
     cannot break anything else.

  4. `proxy_pass http://$upstream/api/` with a variable needs a resolver, so
     the default upstream is a literal IP:port resolved at install time. A
     literal upstream needs no resolver and cannot fail DNS at request time.
"""

import os
import re
import shutil
import time

import core
from core import (B, C, E, G, NGINX_PREFIX, NGINX_PORTAL_PREFIX, PLUGIN_VERSION,
                  R, Y, die, inspect, say, sh)

MANAGED_BEGIN = "# >>> recorder-plugin"
MANAGED_END = "# <<< recorder-plugin"

STANDALONE_PORT_DEFAULT = 18085
CONTAINER_NAME = "record-plugin-static"
CONTAINER_CONF_ROOT = "/etc/nginx/conf.d"
CONTAINER_WWW_ROOT = "/etc/nginx/www/recording"

DEFAULT_SERVER_FILE = "00-recorder-plugin.conf"
LOCATIONS_FILE = "includes/recorder.locations.inc"


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def render_locations(api_host, api_port, portal_host, portal_port, prefix=NGINX_PREFIX,
                     portal_prefix=NGINX_PORTAL_PREFIX):
    """The only generated fragment that contains `location` directives."""
    return """{begin} {version} locations
# Generated file. Regenerate instead of editing:
#   bash install.sh --repair <answers.env>
# This fragment MUST be included from inside a `server{{ }}` block.
# Dropping it into conf.d/ directly makes nginx fail at http{{}} scope.

# --- static UI ------------------------------------------------------------
location = {prefix} {{
    return 308 {prefix}/;
}}

location {prefix}/ {{
    alias {www}/;
    index index.html;
    try_files $uri $uri/ {prefix}/index.html;
    add_header Cache-Control "no-store" always;
}}

# --- recorder API ---------------------------------------------------------
# Trailing slash on proxy_pass maps {prefix}/api/record/start
# to /api/record/start on the controller. Removing it breaks every call.
location ^~ {prefix}/api/ {{
    proxy_pass http://{api_host}:{api_port}/api/;
    proxy_http_version 1.1;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    # The Matrix access token rides in this header; it must reach the
    # controller unchanged or every start is rejected as "not signed in".
    proxy_set_header Authorization     $http_authorization;
    proxy_buffering        off;
    proxy_read_timeout     120s;
    proxy_connect_timeout  5s;
    client_max_body_size   4m;
}}

# --- recordings portal ----------------------------------------------------
location = {portal_prefix} {{
    return 308 {portal_prefix}/;
}}

location {portal_prefix}/ {{
    proxy_pass http://{portal_host}:{portal_port}/;
    proxy_http_version 1.1;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering          off;
    proxy_max_temp_file_size 0;
    send_timeout             3600s;
}}

location /files/ {{
    proxy_pass http://{portal_host}:{portal_port}/files/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_buffering          off;
    proxy_max_temp_file_size 0;
    send_timeout             3600s;
}}
{end} {version}
""".format(begin=MANAGED_BEGIN, end=MANAGED_END, version=PLUGIN_VERSION,
           prefix=prefix, portal_prefix=portal_prefix,
           www=CONTAINER_WWW_ROOT, api_host=api_host, api_port=api_port,
           portal_host=portal_host, portal_port=portal_port)


def render_standalone_server(port, locations_rel=LOCATIONS_FILE, prefix=NGINX_PREFIX,
                             portal_prefix=NGINX_PORTAL_PREFIX):
    """A self-contained `server{}` for the plugin's own nginx container."""
    return """{begin} {version} standalone server
# Generated file. Owned by the plugin; regenerate with --repair.
server {{
    listen {port} default_server;
    server_name _;

    add_header XRecorderPlugin "{version}" always;
    add_header XContentTypeOptions "nosniff" always;

    # The front proxy (or the operator) needs a cheap liveness probe that
    # never touches the controller.
    location = /healthz {{
        access_log off;
        default_type text/plain;
        return 200 "ok\\n";
    }}

    location = / {{
        return 302 {prefix}/;
    }}

    include {conf_root}/{locations};
}}
{end} {version}
""".format(begin=MANAGED_BEGIN, end=MANAGED_END, version=PLUGIN_VERSION,
           port=port, prefix=prefix, conf_root=CONTAINER_CONF_ROOT,
           locations=locations_rel)


def render_front_include(locations_rel, conf_root=CONTAINER_CONF_ROOT):
    """One line the operator pastes into their own `server{}` block.

    `conf_root` is the directory as *nginx* sees it: the in-container path when
    the front proxy runs in a container, the host path when nginx is a host
    binary. Using the wrong one yields a clean `nginx -t` on the operator's
    machine and a 404 for everybody else.
    """
    return ("    # Matrix Element Call Recorder\n"
            "    include %s/%s;\n" % (conf_root.rstrip("/"), locations_rel))


# ---------------------------------------------------------------------------
# upstream resolution
# ---------------------------------------------------------------------------

def resolve_upstreams(a, ctx):
    """(api_host, api_port, portal_host, portal_port) as nginx will see them.

    Container mode: resolve the controller/portal container IP on the shared
    docker network once, and write it as a literal — no resolver needed and no
    runtime DNS failure. Host mode: 127.0.0.1 and the published ports.
    """
    api_port = a.i("CONTROLLER_PORT", 8765)
    portal_port = a.i("REC_HTTP_PORT", 8899)
    net = ctx.get("network") or ""

    if ctx.get("nginx_kind") == "container":
        api_ip = core.container_ip_on("rec-controller", net) if net else ""
        portal_ip = core.container_ip_on("rec-portal", net) if net else ""
        if api_ip and portal_ip:
            say("ok", "upstream: rec-controller %s:%d, rec-portal %s:%d (net %s)"
                % (api_ip, api_port, portal_ip, portal_port, net))
            return api_ip, api_port, portal_ip, portal_port
        raise UpstreamUnresolved(
            "cannot resolve rec-controller/rec-portal on network %r. "
            "Both containers must be running and attached to that network." % net)

    return "127.0.0.1", api_port, "127.0.0.1", portal_port


class UpstreamUnresolved(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# file surgery
# ---------------------------------------------------------------------------

def strip_managed(text):
    """Remove a previously generated block. Returns (text, had_block)."""
    start = text.find(MANAGED_BEGIN)
    if start < 0:
        return text, False
    end_marker = text.find(MANAGED_END, start)
    if end_marker < 0:
        return text, False
    nl = text.find("\n", end_marker)
    end = len(text) if nl < 0 else nl + 1
    while start > 0 and text[start - 1] == "\n" and text[start - 2:start] == "\n\n":
        start -= 1
    return text[:start] + text[end:], True


def write_file(path, content, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def backup_file(path):
    if not os.path.isfile(path):
        return ""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = "%s.bak.%s" % (path, stamp)
    shutil.copy2(path, bak)
    return bak


# ---------------------------------------------------------------------------
# container-side validation
# ---------------------------------------------------------------------------

def nginx_test(container=None):
    """`nginx -t` on the host or inside a container. Returns (ok, text)."""
    if container:
        rc, out, err = sh(["docker", "exec", container, "nginx", "-t"], timeout=60)
    else:
        rc, out, err = sh("nginx -t", timeout=60)
    # nginx writes diagnostics to stderr on success and on failure; keep both
    # separate and label them, never glue them into one blob (#RC3 defect).
    text = "\n".join(x for x in (
        ("[stdout] " + out) if out else "",
        ("[stderr] " + err) if err else "") if x)
    return rc == 0, text


def nginx_reload(container=None):
    if container:
        rc, out, err = sh(["docker", "exec", container, "nginx", "-s", "reload"], timeout=60)
    else:
        rc, out, err = sh("systemctl reload nginx 2>/dev/null || nginx -s reload", timeout=60)
    return rc == 0, (out or err or "")


def nginx_dump(container=None):
    """Config tree from `nginx -T`, stdout only (stderr is diagnostics)."""
    if container:
        rc, out, err = sh(["docker", "exec", container, "nginx", "-T"], timeout=90)
    else:
        rc, out, err = sh("nginx -T", timeout=90)
    return out if rc == 0 else ""


# ---------------------------------------------------------------------------
# standalone container
# ---------------------------------------------------------------------------

def ensure_container(name, port, host_conf_root, host_www_root, image="nginx:1.27-alpine"):
    """(Re)create the plugin's own nginx container with both mounts.

    Recreates rather than patches: the container is stateless, and a recreate
    is the only reliable way to add a missing bind mount on an existing one
    (the #2026-11-05 incident: files existed on the host, the mount did not).
    """
    d = inspect(name)
    need = []
    if d:
        mounts = {m.get("Destination"): m.get("Source") for m in d.get("Mounts") or []}
        if mounts.get(CONTAINER_CONF_ROOT) != host_conf_root:
            need.append("conf mount")
        if mounts.get("/etc/nginx/www/recording") != host_www_root:
            need.append("www mount")
        ports = (d.get("NetworkSettings") or {}).get("Ports") or {}
        if not (ports.get("80/tcp") or []):
            need.append("port")
    if d and not need:
        say("ok", "container %s already correct" % name)
        return True

    if d:
        say("info", "recreating %s (%s)" % (name, ", ".join(need)))
        sh(["docker", "rm", "-f", name], timeout=120)
    else:
        say("info", "creating %s" % name)

    cmd = ["docker", "run", "-d", "--name", name,
           "--restart", "unless-stopped",
           "-p", "127.0.0.1:%d:80" % port,
           "-v", "%s:%s:rw" % (host_conf_root, CONTAINER_CONF_ROOT),
           "-v", "%s:%s:ro" % (host_www_root, "/etc/nginx/www/recording"),
           image]
    rc, out, err = sh(cmd, timeout=300)
    if rc != 0:
        # Never leave a half-created container behind: the next run must find
        # a clean slate, not the corpse of this one.
        sh(["docker", "rm", "-f", name], timeout=60)
        raise RuntimeError("docker run failed for %s: %s" % (name, err[-600:]))
    return True


# ---------------------------------------------------------------------------
# the two public entry points
# ---------------------------------------------------------------------------

def apply_standalone(a, ctx, ports):
    """Own nginx container + generated config tree. The default mode."""
    conf_root = os.path.join(a["PLUGIN_DIR"], "nginx", "conf.d")
    www_root = os.path.join(a["PLUGIN_DIR"], "nginx", "www")
    port = ports.get("standalone_port") or a.i("STANDALONE_PORT", STANDALONE_PORT_DEFAULT)

    api_host, api_port, portal_host, portal_port = resolve_upstreams(
        a, dict(ctx, nginx_kind="container"))

    loc_path = os.path.join(conf_root, LOCATIONS_FILE)
    srv_path = os.path.join(conf_root, DEFAULT_SERVER_FILE)
    write_file(loc_path, render_locations(api_host, api_port, portal_host, portal_port))
    write_file(srv_path, render_standalone_server(port))
    say("ok", "nginx config: %s, %s" % (srv_path, loc_path))

    ensure_container(CONTAINER_NAME, port, conf_root, www_root)

    ok, text = nginx_test(CONTAINER_NAME)
    if not ok:
        raise RuntimeError("nginx -t failed inside %s:\n%s" % (CONTAINER_NAME, text))
    say("ok", "nginx -t passed")

    ok, text = nginx_reload(CONTAINER_NAME)
    say("ok" if ok else "warn", "nginx reload: %s" % ("done" if ok else text[-300:]))
    return port


def apply_external(a, ctx, host_conf_root):
    """Integrate into an nginx the operator already runs (container or host).

    Two writes at most: the locations fragment, and an `include` line inserted
    into the one `server{}` block that serves DOMAIN on :443. Anything
    ambiguous is refused, and the include line is printed for manual pasting.
    """
    api_host, api_port, portal_host, portal_port = resolve_upstreams(
        a, dict(ctx, nginx_kind="host"))

    # Where the file lands on the host vs. how nginx refers to it. For a front
    # proxy in a container these differ; for a host nginx they are the same.
    front = ctx.get("front_container") or ""
    include_root = CONTAINER_CONF_ROOT if front else host_conf_root
    inc_root = os.path.join(host_conf_root, "includes")
    loc_host = os.path.join(inc_root, "recorder.locations.inc")
    write_file(loc_host, render_locations(api_host, api_port, portal_host, portal_port))
    say("ok", "locations written: %s" % loc_host)

    container = ctx.get("front_container") or ""
    dump = nginx_dump(container or None)
    target = find_tls_server_file(dump, a["DOMAIN"])
    if not target:
        say("warn", "no nginx server{} for %s:443 was identified automatically"
            % a["DOMAIN"])
        print()
        print("  Add this single line inside the `server {}` block that serves")
        print("  %s on port 443, then reload nginx:" % a["DOMAIN"])
        print()
        print(render_front_include(LOCATIONS_FILE, include_root))
        print()
        return ""

    host_path = map_container_path(target, ctx, host_conf_root)
    if not host_path:
        say("warn", "server block %s is not backed by a host path I can edit" % target)
        print(render_front_include(LOCATIONS_FILE, include_root))
        return ""

    line = render_front_include(LOCATIONS_FILE, include_root).rstrip("\n")
    with open(host_path, encoding="utf-8", errors="replace") as fh:
        original = fh.read()
    cleaned, _had = strip_managed(original)
    if line.strip() in cleaned:
        say("ok", "include already present in %s" % host_path)
        return host_path

    patched = insert_into_server(cleaned, a["DOMAIN"], line)
    if patched is None:
        say("warn", "refusing to edit %s: could not find exactly one :443 "
                    "server block for %s" % (host_path, a["DOMAIN"]))
        print(render_front_include(LOCATIONS_FILE, include_root))
        return host_path

    bak = backup_file(host_path)
    write_file(host_path, patched)
    ok, text = nginx_test(container or None)
    if not ok:
        if bak:
            shutil.copy2(bak, host_path)
            nginx_reload(container or None)
        else:
            os.unlink(host_path)
        raise RuntimeError("nginx -t rejected the generated include; previous "
                           "config restored.\n%s" % text)
    say("ok", "nginx -t passed after include injection")
    ok, text = nginx_reload(container or None)
    say("ok" if ok else "warn", "nginx reload: %s" % ("done" if ok else text))
    return host_path


# ---------------------------------------------------------------------------
# front-proxy config parsing (stdout only)
# ---------------------------------------------------------------------------

def _server_names(body):
    """All names from every `server_name` directive in a block body."""
    names = []
    for m in re.finditer(r"(?:^|[;{}\s])server_name\s+([^;]+);", body):
        for name in m.group(1).split():
            name = name.strip().strip('"').strip("'")
            if name and name not in ("_", ""):
                names.append(name)
    return names


def _servers(text):
    """Yield (body_start, body_end, body, is_tls, names) for every server{}."""
    out = []
    idx = 0
    while True:
        pos = text.find("server", idx)
        if pos < 0:
            break
        # `server_name`, `server_tokens` etc. are directives, not blocks.
        after = text[pos + 6:pos + 7]
        if after and (after.isalnum() or after in "_"):
            idx = pos + 6
            continue
        brace = text.find("{", pos)
        if brace < 0:
            break
        if text[pos + 6:brace].strip():
            idx = brace + 1
            continue
        depth, i = 1, brace + 1
        while i < len(text) and depth:
            ch = text[i]
            if ch == "#":
                nl = text.find("\n", i)
                i = len(text) if nl < 0 else nl
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            i += 1
        body = text[brace + 1:i - 1]
        listens = re.findall(r"(?:^|[;{\s])listen\s+([^;]+);", body)
        is_tls = any("443" in spec or "ssl" in spec for spec in listens)
        out.append((brace + 1, i - 1, body, is_tls, _server_names(body)))
        idx = i
    return out


def find_tls_server_file(dump, domain):
    """Path of the config file whose one and only :443 server{} serves domain."""
    if not dump:
        return ""
    matches = []
    for chunk in dump.split("# configuration file ")[1:]:
        header, _, body = chunk.partition("\n")
        path = header.rstrip(":").strip()
        hits = [s for s in _servers(body) if s[3] and domain in s[4]]
        if hits:
            matches.append((path, len(hits)))
    exact = [m for m in matches if m[1] == 1]
    if len(exact) == 1:
        return exact[0][0]
    return ""


def insert_into_server(text, domain, line):
    """Insert `line` before the closing brace of the domain's :443 server.

    Returns None whenever the answer is not unambiguous. Ambiguity is the
    dangerous case: pasting the include into the wrong server{} silently gives
    a 404 on /recording/ with a perfectly clean nginx -t.
    """
    cands = [(start, end) for start, end, _body, is_tls, names in _servers(text)
             if is_tls and domain in names]
    if len(cands) != 1:
        return None
    if line.strip() in text[cands[0][0]:cands[0][1]]:
        return text
    _start, end = cands[0]
    return text[:end] + "\n" + line + "\n" + text[end:]


def map_container_path(container_path, ctx, host_conf_root):
    """Translate a path seen by nginx into the host path for the same file."""
    front = ctx.get("front_container") or ""
    if front:
        for container_dir in ("/etc/nginx",):
            host_dir = core.container_mount_for(front, container_dir)
            if host_dir and container_path.startswith(container_dir + "/"):
                rel = container_path[len(container_dir) + 1:]
                return os.path.join(host_dir, rel)
        return ""
    if container_path.startswith(CONTAINER_CONF_ROOT):
        rel = container_path[len(CONTAINER_CONF_ROOT) + 1:]
        return os.path.join(host_conf_root, rel)
    return container_path if os.path.isfile(container_path) else ""
