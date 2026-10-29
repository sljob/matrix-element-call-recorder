# How to test this plugin before publishing

The problem: your test server already has recording installed, and a second clean server
is expensive. Five options follow, cheapest first. You do not need all of them — options
0, 1 and 2 answer different questions and together cover this plugin well.

| Option | Time | What it proves | What you need |
|---|---|---|---|
| 0. No server | 5 min | syntax, compose rendering, refusals | a git clone |
| 1. Fake stack | 10 min | discovery, routing, button injection | Docker on any machine |
| 2. Adopt mode on your test server | 15 min | routes and button over a real recorder | your existing server |
| 3. Clone of the test server without recording | 30–60 min | full install and a real recording | VM clone or host backup |
| 4. Clean VM | 2–3 h | install onto a server that never saw the plugin | any 4 GB VPS |

---

## Option 0. No server at all (5 minutes)

Starts nothing. Checks that the files are not broken.

```bash
cd matrix-element-call-recorder-plugin
bash -n install.sh tools/fetch-upstream.sh testbed/smoke.sh
python3 -m py_compile installer/install.py src/controller/app.py \
    src/portal/index.py src/publisher/publish.py src/adopt/proxy.py
node --check src/ui/record-button.js
node --check src/recorder/server.js
python3 -m json.tool locales/en.json >/dev/null && echo "locales ok"
```

Now confirm the installer **refuses** instead of doing half the work:

```bash
# empty DOMAIN -> non-zero exit
sed 's/^DOMAIN=.*/DOMAIN=/' answers.example > /tmp/a1.conf; chmod 600 /tmp/a1.conf
sudo bash install.sh --check /tmp/a1.conf; echo "rc=$?  (expect non-zero)"

# world-readable answers -> refused before Python runs
cp answers.example /tmp/a2.conf; chmod 644 /tmp/a2.conf
sudo bash install.sh --check /tmp/a2.conf; echo "rc=$?  (expect 1)"

# branch instead of a SHA -> refused
RECORDER_UPSTREAM_COMMIT=main bash tools/fetch-upstream.sh; echo "rc=$? (expect 1)"
```

---

## Option 1. Fake stack: nginx + Element Web (10 minutes)

The most useful test for *this* plugin, because the plugin is almost entirely integration:
find nginx, find its network, find Element Web, inject the button, add the route. None
of that needs Matrix.

The stand listens on 8090/8443 so it cannot collide with a real nginx on the same host.

```bash
cd matrix-element-call-recorder-plugin/testbed
docker compose -f compose.fake.yml up -d
sudo bash smoke.sh          # before install: some checks are reported as "not yet"
```

Then the full cycle (smoke uses adopt mode, so the recorder core is not needed):

```bash
sudo bash ../install.sh --install /tmp/answers-smoke.conf
sudo bash smoke.sh          # after install: every check should pass
```

| Check | What it catches |
|---|---|
| fake stack is up | a broken testbed |
| nginx network detected | network autodetection bugs |
| `--check` exits 0 | answers parsing bugs |
| `/recording/health` = 404 before install | proves the route is ours |
| `record-button.js.patch.js` served with the banner | injection and file serving |
| `POST /start` without a token = 401 | **an exposed worker port** — critical |

To see the button yourself: add `meet.localhost` to `/etc/hosts` pointing at the server,
open `https://meet.localhost:8443/` with `Ctrl+Shift+R`, and watch the `[recorder]` lines
in the browser console.

Tear down:

```bash
sudo bash ../install.sh --uninstall /tmp/answers-smoke.conf
docker compose -f compose.fake.yml down
```

---

## Option 2. Adopt mode on your existing test server (15 minutes)

Your server already has recording. That is not a problem: the plugin can **attach** to an
existing recorder, and in doing so it exercises exactly the parts that differ from the main
installer — discovery, routes and button injection.

What changes on the server: one new container (`rec-adopt-proxy`), two nginx routers and
one `<script>` tag in Element Web's `index.html`. Existing containers are not restarted.
`--uninstall` puts everything back.

**Important:** if you already route `/recording` to an existing controller, the new router
with priority 100 will shadow it. That is expected for a test, but be aware.

```bash
cat > /root/answers-adopt.conf <<'EOF'
DOMAIN=meet.example.org
PLUGIN_DIR=/opt/recorder-plugin
INSTALL_LANGUAGE=ru
RECORDER_ACCOUNTS=@recorder:meet.example.org
RECORDER_PASSWORDS=
TLS_CA_FILE=/opt/element-stack/certs/ca.crt
ADOPT_EXISTING=true
ADOPT_CONTROLLER_URL=http://host.docker.internal:8765
ADOPT_PORTAL_URL=http://host.docker.internal:8899
EOF
chmod 600 /root/answers-adopt.conf

bash install.sh --check   /root/answers-adopt.conf     # read the plan first
bash install.sh --install /root/answers-adopt.conf
curl -sk https://meet.example.org/recording/health | jq
bash install.sh --uninstall /root/answers-adopt.conf
```

If your controller listens somewhere other than `127.0.0.1:8765`, change
`ADOPT_CONTROLLER_URL`. Find out with `ss -lntp | grep 8765`.

---

## Option 3. Clone of the test server with recording disabled (30–60 minutes)

Closest to reality. You need a server where recording does **not** exist yet. Use a VM
snapshot or clone if the server is virtualised; on bare metal use a host backup restored
to a different machine. **Never on the production host.**

On the clone only:

```bash
systemctl stop rec-recorder.service rec-http.service rec-publish.service rec-watch.service 2>/dev/null || true
systemctl disable rec-recorder.service rec-http.service rec-publish.service rec-watch.service 2>/dev/null || true
docker stop recorder-p44 recording-controller record-plugin-static 2>/dev/null || true
docker rm   recorder-p44 recording-controller record-plugin-static 2>/dev/null || true

# the rest must stay alive:
curl -sk https://meet.example.org/_matrix/client/versions | head -c 120; echo
docker ps --format '{{.Names}}' | grep -E 'synapse|nginx|element|livekit'
```

Now it is exactly the server this plugin was written for:

```bash
export RECORDER_UPSTREAM_COMMIT=<sha>
bash tools/fetch-upstream.sh
bash install.sh --check   /root/answers-plugin.conf
bash install.sh --install /root/answers-plugin.conf
```

If you still have the old recorder's `profile-spk`, you **can** copy it into
`<PLUGIN_DIR>/profile/<user_safe>/` before the first run: then no Matrix login is needed
and the E2EE keys are preserved.

### Acceptance scenario: two machines, two accounts

1. Account A (power level ≥ 50) creates a room and starts a call.
2. Press Record. The recorder must join as a participant within 3–5 s.
3. Talk for 30–60 s, share the screen, talk for another 20 s.
4. Press Stop. The button goes to "Processing…" and then back to "Record".
5. Open Recordings: the file is listed, the MP4 plays, the MP3 downloads, a JPG exists.
6. Account B, **not** a member: open the file URL → must be 403.
7. Account B joins at power level 0 → sees only their own rooms in the archive and
   **cannot** start a recording (403 from the controller).
8. Account B with a forged body: `curl -k -X POST .../recording/api/record/start
   -H 'Content-Type: application/json' -d '{"roomId":"!...","powerLevel":100}'` without
   an Authorization header → 401. With B's token → 403. This proves the controller does
   not trust the request body.

```bash
ffprobe -v error -show_entries format=duration:stream=codec_type,codec_name \
        -of default=nw=1 /opt/recorder-plugin/public/*/*.mp4 | head -20
```

Expect a video and an audio stream, duration > 0.

---

## Option 4. Clean VM (2–3 hours, most honest)

Only needed if you want to test on a server that never had Element. A 4 GB VPS and a
domain are enough; `nip.io`/`sslip.io` can replace a real domain
(`meet.203.0.113.10.sslip.io`).

Build a minimal working server with your main installer, then **remove recording from it**
using the commands from option 3, and check that the plugin brings it back. This confirms
the plugin assumes nothing about the host's history.

---

## What counts as "ready to publish"

The minimum before pressing Push:

- [ ] option 0 passes completely;
- [ ] option 1: `smoke.sh` green after `--install`, and `--uninstall` restores
      `index.html` (check `docker exec fake-element-web grep -c recorder-plugin
      /usr/share/nginx/html/index.html` → 0);
- [ ] option 2 or 3: the button is visible in real Element and `/recording/health`
      answers through nginx;
- [ ] option 3, steps 6–8: three refusals obtained (403, 403, 401).

If a box is unticked, say so in the README under "Measured status". Being published is
not proof of having been tested.
