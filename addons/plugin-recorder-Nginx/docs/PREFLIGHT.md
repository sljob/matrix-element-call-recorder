# Pre-install checks and conflict resolution

The installer inspects the environment **before** building or starting anything.
If it finds something that would stop the recorder from working, the install
stops with a readable report instead of a "green" install that dies on the
first recording.

## Running

```bash
bash install.sh --check /root/answers.env     # checks only, changes nothing
bash install.sh --install /root/answers.env   # checks, then installs
```

`--check` exits `2` when blockers are present. `--install` exits `3` and leaves
the server untouched.

Deliberately overriding blockers is a separate key, not tied to `--yes`:

```bash
FORCE_INSTALL="true" bash install.sh --install /root/answers.env
```

## Severity

| Level | Meaning |
|---|---|
| `XX` blocker | recording cannot work; install stopped |
| `!!` warning | it will run, but probably not as intended |
| `..` note | expected leftover from a previous install, handled |
| `OK` verified | nothing to do |

## What is checked

### Bot accounts

The main check. The installer used to ask "does the account exist", which is not
the same question as "will it let us in". An account could exist with a
different password: the check passed, the install completed, and the first
recording failed with `M_FORBIDDEN` inside the worker, where the controller log
shows nothing.

Every bot now performs a real `POST /_matrix/client/v3/login`:

| Situation | Level |
|---|---|
| account exists, answers password works | `OK` |
| account exists, answers password rejected | **blocker** |
| account exists, `RECORDER_BOT_PASSWORDS` empty | **blocker** — password unknown |
| account missing, password set | `!!` — will be created |
| account missing, no password | **blocker** |
| bot equals `ADMIN_USER` | **blocker** |
| two bots share a password | **blocker** |
| Synapse unreachable | `!!` |
| `429` rate limited | `!!` — deliberately not blocking |

`429` is a warning, not a blocker: a check must not create the condition it
looks for. Repeated `--check` runs may hit the login rate limit.

### Ports

`CONTROLLER_PORT`, `REC_HTTP_PORT`, and one port per worker
(`RECORDER_BASE_PORT`, `+1` each) are tested.

| Situation | Level |
|---|---|
| free | `OK` |
| held by our own `rec-*` container | `..` — will be recreated |
| held by a foreign container | **blocker**, with its name |
| held by a host process | **blocker**, with name and pid (`ss -lntpH`) |

### Network and components

| Situation | Level |
|---|---|
| plugin network missing | **blocker** |
| Element Web network missing | `!!` — containerised nginx cannot resolve services |
| Element Web container not found | **blocker** |
| Synapse not found | `!!` — accounts cannot be verified |
| Element Call not found | `!!` |

### Images and leftovers

| Situation | Level |
|---|---|
| `rec-*` containers from a previous install | `..` |
| stray `rec-*` containers (not our names) | `!!` |
| `recorder-plugin/*` images of other versions | `..` |
| more than 5 dangling images | `!!` |

### Templates

The installer renders compose and the nginx snippet into a scratch directory and
parses the result. This catches defects in the installer itself before they
reach disk.

| Situation | Level |
|---|---|
| template raises | **blocker** |
| `{placeholders}` left unrendered | **blocker** |
| compose is not valid YAML | **blocker** |
| fewer than two services | **blocker** |
| traefik labels still present | **blocker** |
| unbalanced braces in the nginx snippet | **blocker** |
| `proxy_pass` or block markers missing | **blocker** |

### Recorder core

| Situation | Level |
|---|---|
| `p63-observer.js` missing | **blocker** |
| known fixes absent (`REC_USER`, `REC_PASS`, `ROOM_ID`, `OUT_FILE`, `-vsync cfr`, `/tmp/ffprogress.log`, `MAP ` with a space, `virtual_speaker.monitor`) | **blocker** — stale core |
| `vendor/` differs from `src/recorder/` | `!!` — a patch landed in one file only |

### nginx

| Situation | Level |
|---|---|
| `nginx -t` fails | **blocker** |
| nginx not in PATH | `!!` |
| another config already serves `/recording/` with `proxy_pass` | **blocker** — nginx takes the first block |

### answers

| Situation | Level |
|---|---|
| duplicate key | **blocker** — the last value silently wins |
| unknown key (typo) | `!!` |
| `DOMAIN` empty | **blocker** |
| `DOMAIN` includes a scheme | **blocker** |
| `DOMAIN` looks like a placeholder | `!!` |
| `PLUGIN_DIR` is `/`, `/etc`, `/usr`, `/root`, `/var`, `/tmp` | **blocker** |
| `ADOPT_EXISTING` with `PUBLISH_LOOPBACK_PORTS=true` | `!!` |

### Resources

| Situation | Level |
|---|---|
| RAM below `2 GB × workers + 2 GB` | `!!` |
| less than 5 GB free on `/` or `/var/lib/docker` | `!!` |

## Resolving a bot account conflict

When the check says the password is rejected or unknown, there are three ways
out. The tool inspects state and performs the chosen action:

```bash
# 1. See what is actually on the server (changes nothing)
bash tools/resolve-bot-accounts.sh --users bot1 --inspect

# 2. Set a known password on existing accounts and write it to answers
bash tools/resolve-bot-accounts.sh --users bot1 \
     --reset 'NewPassword123!' --set-env /root/answers.env

# 3. Delete the accounts so the installer recreates them
bash tools/resolve-bot-accounts.sh --users bot1 --delete
```

The password is changed through the Synapse admin API
(`PUT /_synapse/admin/v2/users/<user>`); the old password is not needed. An
admin access token is required: the script looks one up in `access_tokens`
where `admin = 1`, or accepts `ADMIN_TOKEN=...`.

What deletion costs:

* deactivation ends the bot's sessions and erases its device with E2EE keys;
* `profile-spk` stays on disk, but the bot rejoins as a new device, which room
  members will see as a new session;
* recorded files are untouched.

`--reset` is therefore preferable: it changes only the password and keeps the
bot's device, so the E2EE session survives.

## Full report example

```
============================================================================
  PRE-INSTALL CONFLICT CHECK
============================================================================

BLOCKERS (2)
  XX BOT      @recbot1:example.org exists, but the answers password is rejected
             reset the password and update RECORDER_BOT_PASSWORDS,
             or delete the account: tools/resolve-bot-accounts.sh --delete recbot1
  XX PORT     controller: 8765 held by process nginx (pid 812)
             ss -lntp | grep :8765
             free the port or change CONTROLLER_PORT

WARNINGS (1)
  !! RES      RAM 1.9 GiB, about 4 GiB needed
             reduce RECORDER_BOT_COUNT or add memory

VERIFIED (7)
  OK CTR      Element Web: element-element-1
  OK CTR      Synapse: http://element-synapse-1:8008
  OK NET      plugin network: element_default
  OK PORT     worker-1: 8788 free
  OK TPL      compose valid, 4 services
  OK OBS      observer contains all known fixes
  OK NGX      nginx -t passes

----------------------------------------------------------------------------
  blockers 2 · warnings 1 · verified 7
  Install blocked. Fix the blockers or set FORCE_INSTALL="true" deliberately.
============================================================================
```

## Testing the checks

Every check is a plain function over data, so it is exercised against fixtures
without Docker. Scenarios that must match expectations:

| # | Scenario | Expected |
|---|---|---|
| A | account exists, wrong password | block |
| B | account exists, correct password | pass |
| C | account missing, empty password | block |
| D | account missing, password set | pass, warn |
| E | account exists, no password at all | block |
| F | Synapse unreachable | pass, warn |
| G | bot equals `ADMIN_USER` | block |
| H | port held by a foreign process | block |
| I | port free | pass |
| J | duplicate answers key | block |
| K | typo in a key | pass, warn |
| L | `DOMAIN` with a scheme | block |
| M | `PLUGIN_DIR=/etc` | block |
| N | clean answers | pass |
| O | stale observer | block |
| P | current observer | pass |
| Q | template raises | block |

The fake Synapse used for A–F answers `/profile` and `/login` exactly like the
real one, including `M_FORBIDDEN` on a wrong password.
