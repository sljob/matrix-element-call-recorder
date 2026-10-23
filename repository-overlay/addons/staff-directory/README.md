# Staff directory

## Optional staff directory — 0.1.6

Server-only addon: no endpoint agents. Creates one non-admin bot and one private, intentionally unencrypted Staff (Сотрудники) room. Only joined room members are listed; the admin invites employees. The bot does not join other chats or calls. Only bot PL100 can post messages/reactions. Room admin PL99 can invite employees, but cannot post or change state requiring PL100. Root/Synapse server admin powers remain unaffected.

### New server installation
```ini
STAFF_DIRECTORY_ENABLED=false
```
In answers before first installation: false/omitted skips the addon; true installs after the main stack succeeds. The bootstrap validates selection before the main engine; --check creates no accounts/rooms. DOMAIN/INSTALL_DIR come from answers. Interactive domain confirmation and hidden administrative access token input are required. Never put tokens in answers. Requires Python>=3.10, systemd>=250 with systemd-creds/systemd-run/systemd-analyze. No automatic OS upgrades.

### Existing server without the addon

Run `bash addons/staff-directory/install.sh` from a complete local checkout, or the self-contained standalone.sh in that directory. Never rerun the main installer on production or edit immutable answers.conf. Adds only three addon directories (/opt, /etc, /var/lib/matrix-staff-directory) and three systemd services, without restarting existing containers or changing compose, ports or recorder profile. Requires existing compose.json, CA and synapse/jwt/livekit services. New Matrix bot/room creation is not transactional: after failure new entities may remain; encrypted progress.cred records their IDs for recovery. Configured addons are not overwritten. Multiple independent bot/room instances on one server are not supported.

### Status and device origin

Busy: recognized LiveKit ACTIVE. Online/away: Matrix presence. Otherwise recent local device timestamp within600s gives recently online; expired activity with offline presence gives offline according to Matrix; errors show unknown. Each line includes last request origin: mobile/PC. Select maximum last_seen_ts, excluding dehydrated devices. User-Agent classification is a heuristic, not a device-name guess. Unknown clients are labelled unknown; equal timestamps with distinct recognized types are labelled multiple device types. Background sync counts: this is not call-device detection, attention, screen-lock detection or proof of remote working.

Intervals: publisher/presence5s, LiveKit adapter5s, device collector15s. Recent window10min. Server timestamp updates, API time and client delivery add latency: no real-time guarantee. For N members, up to12N presence+4N device requests per minute plus LiveKit. A publication changes when the result changes or on the15min heartbeat, not every5s. A timestamp older than20min is stale. API failures/large membership increase cycle time; monitor load. Services run as root and Docker adapter access is root-equivalent.

### Credentials and disabling

Bot/admin tokens and LiveKit secrets use encrypted systemd host-key credentials (not TPM). Collector gets admin; publisher gets bot/LiveKit only. This is a full Synapse admin token, not a read-only scope; use an independently revocable session. Encryption at rest does not protect against root/process compromise. The host key is separate at /var/lib/systemd/credential.secret; do not transplant .cred files between servers. config.json contains no tokens.

Disable only addon: `bash /opt/matrix-staff-directory/disable-addon.sh`. Bot/room/history remain. Not a transactional Matrix rollback. Do not remove state directories or rerun the main stack installer after addon failure.

### Validation status

Operator confirmed Cloud INSTALL OK and patch26 on13 October2026: ROOM OK, fresh collector, VERIFY OK readback and three active services. Desktop/mobile classification worked. Earlier Meet status logic passed4/4. Cloud four-stage acceptance has not been supplied; no fresh VM deployment with STAFF_DIRECTORY_ENABLED=true was performed. New0.1.6 first installation creates bot-only permissions directly and is tested programmatically, not claimed fully end-to-end-tested. Optional acceptance: `bash addons/staff-directory/acceptance.sh` as root with interactive terminal (message/call/leave/disconnect all account devices10–15min). Do not reinstall or reapply patch26 on current Cloud.

patches/26_cloud_acl_origin_fast.sh preserves the successfully applied patch for ORIGINAL unmodified Cloud0.1.5 only. It uses admin self-demotion before bot ACL changes because equal-power users cannot demote each other. Not a standalone installer or a repeatable migration; new0.1.6 provisioning does not need this step.

Publish bootstrap, addon and sources.json in one commit. SOURCE_COMMIT must identify the complete source commit. Publication is not proof of fresh-VM validation. Addon status text is currently Russian, independently of INSTALL_LANGUAGE. Desktop publication guide: addons/staff-directory/GITHUB-DESKTOP.ru.md.

### Managing Staff room members

The room administrator at PL99 can **invite and remove members below PL99**, but cannot post messages/reactions. New provisioning uses `invite=50`, **`kick=99`**, messages/reactions=100, bot=100 and room admin=99. The admin cannot remove another PL99 user or the PL100 bot. Ban (`ban=100`) and redaction (`redact=100`) permissions are not relaxed. Removing someone from this room does not deactivate their server account.

In Element: People → select a member → **Remove from room**. For a pending invitation, cancel the invitation. Removed users disappear from the directory on the next refresh; past membership events and messages remain in history. **Deactivate user** is a server-wide action, not the way to remove a member from Staff.

If clicking Record accidentally adds Recorder, stop any active recording and wait for finalization before removing it. **Do not deactivate Recorder**, which is needed by other calls. Removal does not prevent the recording button from inviting it again; blocking recording specifically in Staff is not part of this fix.

For an older configured addon with `kick=100`, run **`bash addons/staff-directory/enable-member-removal.sh`** from a local checkout. It changes only kick to99, saves permissions and verifies readback; it removes nobody and restarts no services/containers. If already99, no change is made. Use the printed `--restore BACKUP_DIR` command to restore, which refuses to overwrite externally changed rights. **Current Cloud already received script27; do not reapply unnecessarily.** On14 October2026 the operator confirmed kick99 readback and the removal action in Element.
