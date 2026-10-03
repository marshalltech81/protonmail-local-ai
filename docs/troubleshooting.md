# Troubleshooting

Diagnostics and recovery steps for a running stack. For first-time
installation and configuration, see [`setup.md`](setup.md).

## Which layer is failing

A healthy Bridge container only means its IMAP port accepts TCP
connections. It does not mean TLS works, an account is logged in, or
mail is syncing. Each layer has its own signal; the states and signals
are defined in
[architecture.md](architecture.md#health-and-readiness-signals). Start
with `make status`, which shows container health and the
`get_mailbox_status` fields, then find the symptom below. Only the
authentication row involves a credential: Bridge's `info` prints the
Bridge password, so treat its output and `.secrets/bridge_pass.txt` as
secrets. No other check needs or shows one.

| Symptom | Failing layer | Next safe check |
| --- | --- | --- |
| `make up` fails with `container protonmail-bridge is unhealthy`, or Bridge restarts | Bridge listening | `docker compose logs protonmail-bridge`; see [Bridge is up but IMAP is unresponsive](#bridge-is-up-but-imap-is-unresponsive--mbsync-cant-connect) and the Bridge start-up sections below |
| mbsync logs `Waiting for ProtonBridge IMAP` with no `Bridge IMAP port is reachable` after it, then `Bridge IMAP did not become reachable` | Bridge listening, as mbsync sees it | Default mode: the Bridge container's health and logs. macOS Bridge mode: [the app is running and its port matches](#macos-bridge-mode-mbsync-cannot-reach-or-verify-the-bridge-app) |
| mbsync stops with `cert extraction failed` | TLS handshake | Run the TLS probe in [Confirm Bridge IMAP answers over TLS](#confirm-bridge-imap-answers-over-tls); a Bridge image from before implicit TLS needs `make build`. macOS Bridge mode: [the app's IMAP mode must be SSL](#macos-bridge-mode-mbsync-cannot-reach-or-verify-the-bridge-app) |
| mbsync stops with `Bridge cert fingerprint does not match pinned value` or `does not match BRIDGE_CERT_FINGERPRINT` | TLS identity | [Bridge cert pin mismatch](#mbsync-refuses-to-sync--bridge-cert-pin-mismatch) |
| mbsync is healthy, logs `Initial sync returned a non-zero status` or `Sync failed (n/5 consecutive failures)`, restarts after five; `get_mailbox_status` says `no successful mail sync has been recorded` | Bridge authentication, or the sync itself | Check that `BRIDGE_USER` and `.secrets/bridge_pass.txt` match Bridge's `info` (macOS Bridge mode: the app's IMAP details) and that an account is logged in ([re-authenticate](#bridge-credentials-expired--need-to-re-authenticate); in macOS Bridge mode, in the app); `mbsync -V` in [Verifying mbsync is working](#verifying-mbsync-is-working) names the failing step |
| `make up` fails with `container mbsync is unhealthy` | mbsync syncing | [`make up` fails — mbsync is unhealthy](#make-up-fails--mbsync-is-unhealthy) |
| mbsync is healthy, `get_mailbox_status` says `last successful mail sync was ... ago` | Last successful sync (recent syncs failing, or one long run in progress); or the indexer has not acknowledged a newer stamp (see the indexer rows) | `docker compose logs mbsync --tail 50`: repeated `Sync failed` lines, or no `Syncing...` since a long run started ([deadline](#mbsync-stopped-a-sync-at-its-deadline)) |
| `get_mailbox_status` says `the indexer has not reported` or `the indexer last reported ... ago` | Index current: indexer down or stalled | `docker compose logs indexer --tail 50` and `docker inspect indexer --format='{{json .State.Health}}'` |
| `get_mailbox_status` says `... waiting to be indexed` | Index current: indexing behind | Normal after a large sync; if it does not fall, see [Tuning indexing retries](#tuning-indexing-retries) |
| `get_mailbox_status` is current but a message is missing | Index: a dead-lettered message (`current` ignores the `dead` count), or none: the mail reached Proton after the last sync | If the `dead` count is non-zero, fix its cause and run `make requeue-dead` (see [Tuning indexing retries](#tuning-indexing-retries)); otherwise wait one `SYNC_INTERVAL` |

## Bridge won't start — "Failed to launch exit status 1"

This can happen if the image is outdated. Check:

```bash
docker compose logs protonmail-bridge
```

If the image is outdated, rebuild:

```bash
make bridge-upgrade-check
make update
```

## Bridge won't start — keychain / GPG errors

```bash
docker compose logs protonmail-bridge
```

When `vault.enc` exists, the entrypoint checks the credential chain before
starting Bridge: the `ProtonBridge` GPG private key is present, the pass
store's `.gpg-id` names that key, and Bridge's vault key entry
(`docker-credential-helpers/.../bridge-vault-key.gpg` under `/data/pass`)
exists and decrypts. If any check fails it exits with `ERROR: vault.enc exists but ...` and changes nothing; it
never generates a replacement key or re-initializes pass over an existing
vault, because Bridge's vault key is stored in that pass store. Key generation
and `pass init` run only when there is no vault yet.

Restore the `bridge-data` volume from a backup if you have one. Otherwise the
GPG/pass store is unrecoverable: wipe the bridge data volume and re-run
first-run:

```bash
docker compose down
docker volume rm protonmail-local-ai_bridge-data
make first-run
```

This is a full re-authentication: finish with the remaining steps in
[Bridge credentials expired / need to re-authenticate](#bridge-credentials-expired--need-to-re-authenticate),
including the mbsync cert pin rotation.

## Bridge drops to the interactive CLI on every `make up`

`make first-run` always opens the interactive CLI. Under `make up`, the
entrypoint starts Bridge noninteractively when the bridge-data volume holds
`vault.enc`, and opens the CLI otherwise. If `make up` keeps dropping to the
interactive CLI, the volume may not be persisting correctly:

```bash
docker volume inspect protonmail-local-ai_bridge-data
```

Ensure `make first-run` uses `docker compose run` (not `docker run`) so the named
volume is mounted.

If the volume persists but this started after a Bridge upgrade, check the vault
path. `bridge/entrypoint.sh` looks for
`/data/config/protonmail/bridge-v3/vault.enc`, and the `bridge-v3` segment is
tied to Bridge's major version. A Bridge major version that stores its vault
elsewhere fails this check on every restart:

```bash
docker run --rm -v protonmail-local-ai_bridge-data:/data:ro debian:bookworm-slim \
    find /data/config/protonmail -name vault.enc
```

## Startup warnings: "Failed to add test credentials to keychain" / "no vault key found"

These are harmless when they refer to the desktop keychain: Bridge cannot use it
(no dbus session in a container) and uses the GPG-backed `pass` store instead. The
"no vault key found" warning only appears once — on the very first run before the
vault is created. An unusable `pass` store is not harmless, and the entrypoint
refuses to start Bridge over an existing vault when it is; see
[Bridge won't start — keychain / GPG errors](#bridge-wont-start--keychain--gpg-errors).

## Reading Bridge logs directly from the volume

Bridge writes structured logs to a timestamped file inside the `bridge-data` volume.
Since Bridge no longer streams logs to Docker stdout, read them directly:

```bash
docker run --rm \
    -v protonmail-local-ai_bridge-data:/data:ro \
    debian:bookworm-slim \
    bash -c 'find /data/local/protonmail/bridge-v3/logs -name "*.log" | sort | tail -1 | xargs tail -n 100'
```

To follow the log in real time, replace `tail -n 100` with `tail -f`.

## Bridge is up but IMAP is unresponsive / mbsync can't connect

Bridge may still be in the middle of its initial Gluon sync — pulling every
message body from Proton's API into its local database before it can serve IMAP.
This is not the same as mbsync syncing to Maildir. It happens inside the Bridge
container and can take hours on a large mailbox.

Run these three diagnostics to understand what state Bridge is in:

**1. Check recent Bridge logs**

```bash
docker run --rm \
    -v protonmail-local-ai_bridge-data:/data:ro \
    debian:bookworm-slim \
    bash -c 'find /data/local/protonmail/bridge-v3/logs -name "*.log" | sort | tail -1 | xargs tail -n 30'
```

If you see rapid-fire lines like:

```
200 OK: GET https://mail-api.proton.me/mail/v4/messages/<id>
200 OK: GET https://mail-api.proton.me/mail/v4/messages/<id>
```

Bridge is still downloading messages. Do not attempt cert extraction yet —
IMAP will be unresponsive during heavy Gluon sync, and `mbsync` now fails
closed instead of syncing without a pinned Bridge cert. If Bridge stays in
this state, the `mbsync` container now exits after a bounded wait and Docker
restarts it so the failure is visible instead of hanging forever.

**2. Check that Bridge is authenticated**

Neither the container's health nor its files show this. Bridge writes
`vault.enc` at startup, before any login, and it listens, completes TLS and
greets IMAP clients with no account logged in. The signal is mbsync: a
sync that completes (`get_mailbox_status` reports a last sync) proves its
login was accepted, while a rejected login shows as repeated
`Sync failed` lines in `docker compose logs mbsync`. To see which accounts
Bridge holds, stop the stack, run `make first-run` and enter `info` in the
CLI (see [Bridge credentials expired](#bridge-credentials-expired--need-to-re-authenticate));
`info` prints the Bridge password, so keep its output private. In macOS
Bridge mode `make first-run` starts the container Bridge, not the app:
check the account in the Bridge app instead.

**3. Check the bridge binary is actually running**

```bash
docker exec protonmail-bridge ps aux
```

A container can be "Up" while the process inside has crashed. If `bridge` does
not appear in `ps aux`, the process exited — check the logs for the error and
restart the container.

**How to know Gluon sync is finished**

Watch for the log pattern to shift from message fetching to event polling:

```
# Still syncing — rapid fire, sub-second interval:
200 OK: GET .../mail/v4/messages/<id>
200 OK: GET .../mail/v4/messages/<id>

# Sync complete — sparse, several seconds apart:
200 OK: GET .../mail/v4/events/<id>
200 OK: POST .../data/v1/metrics
```

Once you see event polling instead of message fetching, IMAP is fully
responsive. mbsync extracts the Bridge TLS cert itself on its next
start; then check it with "Verifying mbsync is working" below.

### Confirm Bridge IMAP answers over TLS

Bridge serves IMAP with implicit TLS (#638), so a plaintext client such as
`nc` gets no greeting from it. Probe from inside the Bridge container, whose
image already has `openssl`; nothing is published and no credential is sent:

```bash
printf 'a1 LOGOUT\r\n' | docker exec -i protonmail-bridge \
    timeout 20 openssl s_client -connect localhost:1143 -quiet -ign_eof
```

The `verify error:num=18:self-signed certificate` line is expected: this
probe only checks that TLS works and IMAP answers, and trusts nothing (mbsync
checks the certificate against its pin). If IMAP is ready, the TLS lines are
followed by the Bridge greeting and the logout, e.g.:

```
* OK [CAPABILITY AUTH=PLAIN ... IMAP4rev1 ...] Proton Mail Bridge 03.27.00 - gluon session ID 1
* BYE
a1 OK LOGOUT
```

A greeting shows the listener and TLS work, not that an account is logged
in: Bridge greets the same way with no account. A handshake error means the
running Bridge image does not serve implicit TLS (rebuild it with
`make build`, then `make up`). A probe that hangs until `timeout` stops it
means Bridge is still in its initial sync or the process has crashed —
check the logs and process steps above.

## Verifying mbsync is working

Run these checks in order of depth.

**1. Is mbsync running and looping?**

```bash
docker compose logs mbsync --tail 20
```

Look for `>>> Syncing...` lines repeating at your `SYNC_INTERVAL`. If startup
fails, `mbsync` now logs a specific cause such as:

- missing `BRIDGE_USER`
- missing or empty `/run/secrets/bridge_pass`
- cert extraction timeout
- `openssl s_client` handshake errors. mbsync connects with implicit
  TLS only (#638). With the Bridge container this means the running
  Bridge image predates that change, so rebuild it (`make build`, then
  `make up`); in macOS Bridge mode see
  [macOS Bridge mode](#macos-bridge-mode-mbsync-cannot-reach-or-verify-the-bridge-app)
- Bridge TLS cert fingerprint does not match the pinned value (see the
  "Bridge cert pin mismatch" section below)

Repeated sync failures now count toward an exit threshold so the container
restarts instead of looping forever in a broken state. A Proton folder
that was renamed or deleted is not such a failure; see
[A Proton folder was renamed or deleted](#a-proton-folder-was-renamed-or-deleted).

**2. Did any mail land in the Maildir volume?**

```bash
docker run --rm \
    -v protonmail-local-ai_maildir-volume:/maildir:ro \
    debian:bookworm-slim \
    find /maildir -name "*.eml" -o -name "*:2,*" | wc -l
```

A non-zero count means mbsync is writing files. Zero means it connected but
downloaded nothing — either the mailbox is empty or `Patterns` is filtering
everything out.

**3. Check the folder structure was created**

```bash
docker run --rm \
    -v protonmail-local-ai_maildir-volume:/maildir:ro \
    debian:bookworm-slim \
    find /maildir -maxdepth 2 -type d
```

You should see `INBOX`, `Sent`, `Drafts`, etc. If only `/maildir` appears with
nothing under it, the sync ran but Bridge returned no folders.

**4. Force a sync now and watch verbose output**

```bash
docker exec mbsync mbsync -c /tmp/mbsync/mbsyncrc -a -V 2>&1 | head -50
```

`-V` prints each folder being synced and message counts. This is the most
informative test — it will clearly show auth failures, cert errors, or folder
mismatches.

## mbsync fails to connect

Bridge takes 10–15 seconds to fully start. mbsync waits automatically, but it
now gives up after a bounded wait and lets Docker restart it rather than
appearing healthy forever. If it keeps failing:

```bash
docker compose logs mbsync
docker compose logs protonmail-bridge
```

If you want Docker's view of the current state:

```bash
docker inspect mbsync --format='{{json .State.Health}}'
```

## macOS Bridge mode: mbsync cannot reach or verify the Bridge app

In [macOS Bridge mode](setup.md#macos-bridge-mode-optional) there is no
Bridge container to inspect; check the app and the connection instead.

- **`Waiting for ProtonBridge IMAP on host.docker.internal:1143...`
  until mbsync gives up.** The app is not running, is logged out, or
  listens on another port. Open the app, check the account is
  connected, and compare its IMAP port with `BRIDGE_IMAP_PORT` in
  `.env` (default 1143). To probe from the container:
  `docker exec mbsync nc -z -w 2 host.docker.internal 1143`.
- **`Bridge IMAP port is reachable` but `cert extraction failed`, with
  `If Bridge is not serving implicit TLS`.** The app's IMAP connection
  mode is STARTTLS (the app's default) rather than SSL; `openssl
  s_client` then usually reports `wrong version number`. mbsync speaks
  only implicit TLS (#638) and never falls back, so nothing was sent.
  Switch the app's IMAP connection mode to SSL in its settings, take the
  fingerprint again if you have not yet
  ([Set it up](setup.md#set-it-up), step 3; it does not change with the
  mode), and `make up-macos-bridge`.
- **`BRIDGE_CERT_FINGERPRINT is not set`.** This mode does not trust
  the app's certificate on first use, so mbsync stops at startup,
  before it waits for or connects to the app. Take the fingerprint on the Mac
  and set it in `.env`
  ([Set it up](setup.md#set-it-up), step 3), then `make up-macos-bridge`.
- **`the Bridge certificate does not match BRIDGE_CERT_FINGERPRINT`.**
  Compare the `presented:` value with the fingerprint taken on the Mac
  while the app is running. If they differ, something other than the app
  answered on its port (for example another local account's process
  while the app was closed): treat it as a security event. If the app's
  certificate changed on purpose (reinstall, reset), update
  `BRIDGE_CERT_FINGERPRINT` and rotate the pin as in
  [Switching an existing installation](setup.md#switching-an-existing-installation).
- **`certificate owner does not match hostname host.docker.internal`.**
  mbsync was recreated without the overlay's `BRIDGE_CERT_HOST`, or by a
  `docker compose` command missing
  `-f docker-compose.macos-bridge.yml`. Run `make up-macos-bridge`.
- **`certificate owner does not match hostname 127.0.0.1`.** The app
  presents a certificate not issued for `127.0.0.1`, such as one
  imported into Bridge by hand. Use a certificate for `127.0.0.1`; there
  is no option to skip the check.
- **`Bridge cert fingerprint does not match pinned value` after
  switching modes, reinstalling the app, or resetting it.** Expected
  once: the app presents a different certificate. Verify it and rotate
  the pin as in
  [Switching an existing installation](setup.md#switching-an-existing-installation).
- **`UIDVALIDITY genuinely changed` or `Unable to recover from
  UIDVALIDITY change` after switching modes.** mbsync's sync state
  belongs to the previous Bridge. mbsync leaves those folders untouched;
  see [mbsync reports a UIDVALIDITY change](#mbsync-reports-a-uidvalidity-change).

## A Proton folder was renamed or deleted

mbsync never deletes local mail (`Expunge None`) and keeps a local folder
after its Proton folder goes away. A renamed folder syncs again under its
new name; the old local copy stays. On every later sync, mbsync tries to
open the old name on Bridge, cannot, and logs:

```text
>>> WARNING: 1 far-side folder(s) could not be opened, most likely renamed or deleted in Proton; their local copies are kept. Folder names are not logged (see docs/troubleshooting.md).
>>> mbsync reported no other error — counting this sync as successful.
```

This is expected and harmless. The rest of the mailbox keeps syncing, the
sync counts as successful (it does not count toward the restart threshold),
and the last-sync stamp is still written, so `get_mailbox_status` does not
report the mailbox as stale: the old folder has nothing left to pull. The
log holds a count, not the folder names, because folder names are mailbox
content.

The run still counts as a failure, as before, when mbsync reports any other
error in the same run, exits with any status other than 1, or cannot open
`INBOX` (which cannot be renamed or deleted, so Bridge refusing it means
Bridge is refusing folders, not that one went away). The match is the exact
line isync 1.4.4 prints, `Error: channel protonmail: far side box <name>
cannot be opened.`; a different isync version that words it differently
falls back to counting the run as a failure.

To see which local folders are affected, run one sync by hand:

```bash
docker exec mbsync mbsync -c /tmp/mbsync/mbsyncrc -a 2>&1 | grep 'cannot be opened'
```

The warning repeats for as long as the local copy exists. Nothing removes
it automatically. Removing or moving the folder out of the Maildir yourself
stops the warning, but its messages then count as deleted for the indexer
(see [Deletion reconciliation](#deletion-reconciliation-mirror-vs-archive)).

## Folder names in mbsync's log

Folder names are mailbox content, so the entrypoint keeps them out of
`docker logs mbsync` (#570). Every isync 1.4.4 message that names a folder
is logged with the name replaced and the rest of the message kept:

- a folder name becomes `<folder>`, for example
  `Error: channel protonmail, far side box <folder>: UIDVALIDITY genuinely changed (at UID 42).`
  (`INBOX` is shown as it is);
- a path in the Maildir or its sync state, which contains the folder name,
  becomes `<path>`, for example
  `Maildir error: cannot write <path>: No space left on device`;
- where isync does not end the message cleanly (an IMAP command that
  quotes a folder, or a message printed without a line break), the rest of
  the line is cut: `IMAP command 'CREATE <folder>' (rest of line withheld)`;
- text Bridge itself returns, which may name a folder too, is withheld
  after the fixed part, for example
  `Error from IMAP server: (server text withheld)` or
  `IMAP command 'UID FETCH 1:5 (UID FLAGS)' returned an error: NO (server text withheld)`.

Redaction does not change how a sync is counted: these errors still fail
it, and only the far-side `cannot be opened` line
([above](#a-proton-folder-was-renamed-or-deleted)) is tolerated.

To see which folder a message is about, or Bridge's full reply, run one
sync by hand. The output of `docker exec` goes to your terminal and is
not recorded in the container's log:

```bash
docker exec mbsync mbsync -c /tmp/mbsync/mbsyncrc -a
```

Add `| grep UIDVALIDITY` to see only UIDVALIDITY errors. Without running a
sync, the sync state files show which folders mbsync tracks: each folder
keeps one, `.mbsyncstate`, in its own Maildir directory, where every
component after the first has a leading dot (`Folders/.Clients` is
`Folders/Clients`):

```bash
docker exec mbsync find /maildir -name .mbsyncstate
```

A Proton child folder named `uidvalidity`, `isyncuidmap.db`,
`mbsyncstate`, `mbsyncstate.journal`, `mbsyncstate.new` or
`mbsyncstate.lock` is not synced, nor is anything below it: its Maildir
directory would be a file isync keeps in its parent's directory. Nothing
is logged for it. Rename the folder in Proton to sync it.

## mbsync refuses an earlier Maildir layout

```text
>>> ERROR: /maildir was synced with an earlier mbsync layout (sync state at the Maildir root, or subfolders without the leading dot) — refusing to sync.
```

mbsync keeps each folder's sync state in that folder's own directory
(#275) and writes a child folder with a leading dot (#281; see
[Maildir layout](architecture.md#maildir-layout)). A Maildir synced by an
earlier version keeps the state at the Maildir root and its child folders
without the dot, where this version would read neither: it would download
mail a second time next to the copies already there. mbsync therefore
refuses to start, before it connects to Bridge, and changes nothing. The
message names no path, because those paths hold folder names.

If it says instead that it `could not inspect /maildir`, `find` could not
read part of the Maildir (usually a permission problem); it gives only a
count of `find`'s errors, for the same reason. Run the `docker exec`
command it prints to see them in your terminal.

Start the Maildir over. Mail is pulled again from Proton, so nothing is
lost, but remove the index with it: its rows point at the old files, which
would otherwise go through
[deletion reconciliation](#deletion-reconciliation-mirror-vs-archive).
Run these from the checkout, with the project name the stack runs under:

```bash
names=$(docker compose config --format json | python3 -c 'import json, sys
v = json.load(sys.stdin)["volumes"]
print(v["maildir-volume"]["name"], v["sqlite-volume"]["name"])')
make down
docker volume rm $names
make up                # or make up-macos-bridge in macOS Bridge mode
```

To keep a copy of the old Maildir first, back it up as in
[Recover from a genuine change](#recover-from-a-genuine-change).
The Bridge vault and mbsync's certificate pin (the `mbsync-state` volume)
are untouched. mbsync then pulls the whole mailbox, and the indexer
rebuilds the index from it, which re-embeds every message (a cost with a
paid embedding provider).

## mbsync reports a UIDVALIDITY change

Reinstalling or resetting Bridge, re-adding the account, rebuilding the
Bridge container's vault, or switching between the Bridge container and
the macOS Bridge app can leave three pieces of mbsync's state stale. Each
one stops the sync before the next can show, so after such a change they
appear in this order:

| Log line in `docker logs mbsync` | What is stale | Fix |
| --- | --- | --- |
| `Bridge cert fingerprint does not match pinned value`, or in macOS Bridge mode `the Bridge certificate does not match BRIDGE_CERT_FINGERPRINT` | The certificate pin. Checked before mbsync logs in. | Verify the new certificate, then rotate the pin: [Bridge cert pin mismatch](#mbsync-refuses-to-sync--bridge-cert-pin-mismatch), or [macOS Bridge mode](#macos-bridge-mode-mbsync-cannot-reach-or-verify-the-bridge-app). |
| `IMAP command 'LOGIN <user> <pass>' returned an error: NO (server text withheld)` (or `AUTHENTICATE PLAIN <authdata>`, or `BAD`) | The credentials: Bridge refused `BRIDGE_USER` or `.secrets/bridge_pass.txt`. | Copy the new username and password from Bridge: [re-authenticate](#bridge-credentials-expired--need-to-re-authenticate), or step 2 of [Switching an existing installation](setup.md#switching-an-existing-installation). |
| `Error: channel protonmail, far side box <folder>: UIDVALIDITY genuinely changed (at UID 42).` or `... Unable to recover from UIDVALIDITY change.` | The sync state: this Bridge numbers the folder's messages differently. | Below. |

### Spurious or genuine

mbsync records each folder's IMAP UIDVALIDITY and the UID of every
message it pulled, in the folder's `.mbsyncstate` (see
[Folder names in mbsync's log](#folder-names-in-mbsyncs-log)). When
Bridge reports a different UIDVALIDITY, isync 1.4.4 tells the two cases
apart itself, by comparing the Message-ID of each message it pulled with
the message Bridge now serves at that UID:

- **Spurious**: every message it can check is still at its old UID.
  isync accepts the new UIDVALIDITY, logs
  `Notice: channel protonmail, far side box <folder>: Recovered from change of UIDVALIDITY.`
  and syncs as usual. Nothing to do.
- **Genuine**: a UID now holds another message. isync logs
  `UIDVALIDITY genuinely changed (at UID <n>)`.
- **Unknown**: no UID contradicts the old state, but too few messages
  could be confirmed (fewer than 20, and fewer than 80% of those it
  pulled before; typical of Drafts). isync logs
  `Unable to recover from UIDVALIDITY change`. Treat it as genuine.

`UIDVALIDITY of both far side <folder> and near side <folder> changed`,
or a `near side box` line, means the local Maildir's own UIDVALIDITY
changed, which happens when something other than mbsync rewrites the
Maildir (a restore from a backup, for example). Treat it as genuine too.

In each error case isync skips that folder and changes nothing in it
(mbsync is pull-only and never expunges); the other folders keep
syncing. The run counts as a failed sync, and five in a row restart
mbsync. Retrying does not help: the state cannot be reused.

If every folder reports a genuine change right after `BRIDGE_USER`
changed, first check that it names the right account (the Bridge CLI's
`info`, or the account's IMAP details in the Bridge app). Another
account's mailbox looks like a genuine change too.

### Recover from a genuine change

Back up the Maildir, then start it over together with the index. Mail
is pulled again from Proton, and the backup keeps every file the
Maildir held. Run these from the checkout, with the project name the
stack runs under:

```bash
make down
names=$(docker compose config --format json | python3 -c 'import json, sys
v = json.load(sys.stdin)["volumes"]
print(v["maildir-volume"]["name"], v["sqlite-volume"]["name"])')
maildir=${names%% *}
mkdir -m 700 -p ~/protonmail-local-ai-backup
docker run --rm -v "$maildir:/maildir:ro" -v ~/protonmail-local-ai-backup:/backup \
    debian:bookworm-slim bash -c '
        tar -C /maildir -czf /backup/maildir-uidvalidity.tgz . &&
        find /maildir -type f \( -path "*/cur/*" -o -path "*/new/*" \) | wc -l &&
        tar -tzf /backup/maildir-uidvalidity.tgz | grep -cE "/(cur|new)/[^/]+$"'
chmod 600 ~/protonmail-local-ai-backup/maildir-uidvalidity.tgz
```

The two numbers it prints are the message files in the Maildir and in
the archive. Continue only if they match:

```bash
docker volume rm $names
make up                # or make up-macos-bridge in macOS Bridge mode
docker logs mbsync     # no UIDVALIDITY errors
```

What this keeps and changes:

- The archive is the old Maildir as it was, every `.eml` included, and
  is the only copy of mail that Proton no longer has (deleted there but
  kept locally, with the `T` flag). It is your mailbox, unencrypted:
  keep it outside the checkout, as above, so no `git add` can pick it
  up, and on an encrypted disk (FileVault).
- The sync state lives inside the Maildir, so it goes with it. The
  certificate pin (the `mbsync-state` volume) and the Bridge vault are
  untouched.
- mbsync pulls the whole mailbox into the new Maildir, each message
  once, and the indexer rebuilds the index from it, which re-embeds
  every message (a cost with a paid embedding provider). Removing the
  index too keeps the old files' rows from going through
  [deletion reconciliation](#deletion-reconciliation-mirror-vs-archive).
- The new index holds what Proton holds now. Under mirror retention
  (the default) that is where the old index was heading: mail deleted in
  Proton is reaped after the grace window anyway. Under archive mode
  (`INDEXER_DELETION_ENABLED=false`), mail deleted in Proton before the
  recovery is in the archive only and no longer searchable (#603).

`make test-mbsync-layout` runs this against synthetic stores: a spurious
change, a genuine one that fails every sync without changing a local
file, and the fresh Maildir that then holds each message once while the
old one stays whole.

### Why not reset only the sync state

Moving a folder's `.mbsyncstate` files aside (or editing them, or
`.uidvalidity`) makes mbsync sync again, but it does not recover:

- isync then knows none of the local files and downloads every message
  again next to its old copy.
- The copies differ in their bytes: isync writes a random `X-TUID`
  header into each file it stores. The indexer keys a message on its
  Message-ID and a hash of its bytes, so it indexes both, and every
  message appears twice.
- The old copies are no longer tracked: a flag change or deletion in
  Proton reaches only the new copy, so a message deleted in Proton
  stays searchable through its old copy, even under mirror retention.
- Removing the old copies afterwards means deleting `.eml` files by
  guesswork.

`make test-mbsync-layout` checks each of these. A folder-by-folder reset
saves only download time, since the index has to be rebuilt either way,
and the log does not name the folders.

## Embedder or inference endpoint unreachable from containers

The indexer or mcp-server reports a connection error against
`EMBED_BASE_URL` / `INFERENCE_BASE_URL` /
`RERANK_BASE_URL`. The project does not run those
servers, so the diagnostic depends on where you pointed it:

- Host-side server: confirm it is listening on the configured port
  (`lsof -iTCP:<port> -sTCP:LISTEN`) and bound to `127.0.0.1`.
  Containers reach `127.0.0.1` on the host as
  `host.docker.internal:<port>` via OrbStack.
- Remote provider: verify outbound networking from a container:

  ```bash
  docker run --rm curlimages/curl:latest -fsS https://example.com
  ```

  If the hardened compose overlay is active (`internal: true` on
  `app-net`), all remote provider calls are blocked by design.

## Inference / embedder cold start

The first call after a fresh install often triggers a model load on
host-side servers (or a per-provider warmup on remote endpoints).
`EMBED_WARMUP_TIMEOUT_SECS` (default 600) is the HTTP timeout on the
first warmup POST: the indexer fails it when the connection, or the
wait for response data, makes no progress for that long. It is a
per-operation timeout, not a total deadline for the request. Watch the relevant
provider's log for download / load progress.

## sqlite-vec fails with "wrong ELF class: ELFCLASS32" (ARM64 / Apple Silicon)

You are running an older pinned version. `sqlite-vec` versions prior to 0.1.9 ship
an armv7 (32-bit) wheel which is incompatible with aarch64 containers. Ensure
both `indexer/pyproject.toml` and `mcp-server/pyproject.toml` pin
`sqlite-vec==0.1.9` or later, regenerate the lockfiles, then rebuild:

```bash
docker compose build indexer mcp-server
```

## Index is empty after startup

The initial sync may still be running. Check:

```bash
docker compose logs indexer
docker compose logs mbsync
```

The indexer starts while mbsync's first sync is still running, but it cannot
see that sync's mail yet. mbsync creates every folder owner-only (mode 0700)
and makes folders and files readable to the indexer, which runs as a
different user, only when a sync completes. `make status` reports "no
successful mail sync has been recorded" until then.

### Mail from the first sync is indexed only after it completes

The indexer watches the Maildir for new files, but it cannot add a watch to
a folder it could not read when the folder appeared, and making the folder
readable later does not add one by itself. On a fresh install that is every
folder from the first sync, INBOX included. After each sync attempt, once
mbsync has made its files readable and renamed its repair marker
(`.mbsync-perms-repaired`) into place, the indexer checks for directories
that became readable and, if it finds any, re-creates its watch and queues
the mail already in them (#516). No restart is needed. The same applies to a folder
created in Proton while the stack runs: its mail is indexed after the sync
that created it, and its later deliveries in real time.

A sync attempt that fails writes no last-sync stamp but still writes the
marker, so folders it created are watched without waiting for a successful
sync (#524). If the marker cannot be written, mbsync logs a warning and the
folders are watched at the next successful sync or the next recovery sweep
(`INDEXER_RECOVERY_SWEEP_INTERVAL_SECS`, default 30 minutes), whichever
comes first. The check runs in the indexer's main loop. While the
indexer's startup index is still draining a large backlog, it waits until
that finishes; the mail is not lost, only indexed later.

## `make up` fails — mbsync is unhealthy

`make up` waits for mbsync to report healthy before it starts the indexer,
and for the indexer before the MCP server. If it fails with
"dependency failed to start: container mbsync is unhealthy", the indexer
and MCP server were never started, and a later recovery of mbsync does not
start them.

mbsync is healthy while its sync loop is alive: its config and the Bridge
cert are in place, and either a sync attempt started or ended within three
`SYNC_INTERVAL`s (plus 30 s) or an `mbsync` process or the permission repair
walk that follows it (`find`) is running. A long first
sync is therefore healthy; it is not a reason for this failure. A run still
going past its deadline (`SYNC_DEADLINE_SECONDS` plus 60 s) is unhealthy:
see [mbsync stopped a sync at its deadline](#mbsync-stopped-a-sync-at-its-deadline). (Images built
before this behaviour required a completed sync and failed any first sync
longer than about three and a half minutes; rebuild with `make build`.)

What remains unhealthy is mbsync that has not started syncing or has
stopped: Bridge IMAP not reachable yet, cert extraction or the cert pin
refused, or the loop stuck outside a sync. Check why:

```bash
docker compose logs mbsync --tail 50
docker inspect mbsync --format='{{json .State.Health}}'
```

Fix the cause the log names (see "mbsync fails to connect" and the cert pin
sections below). Repeated sync failures make the container exit and restart
after five consecutive failures, so `docker compose ps` shows the restarts.
Once mbsync is healthy, run `make up` again (`make up-macos-bridge` in
[macOS Bridge mode](setup.md#macos-bridge-mode-optional)): Compose leaves the running
services as they are and starts the indexer and then the MCP server.

## Indexer cannot read `config/authority.toml` on Linux

On Linux with Docker Engine the indexer (UID 1002) sees the host owner
and mode of the bind-mounted `config/authority.toml`, so a `600` file
you own stops it at startup with "could not be read". `make up` and
`make restart-indexer` catch this first and print the fix, which grants
UID 1002 alone read access with an ACL (needs the `acl` package):

```bash
setfacl -b config/authority.toml && chmod 600 config/authority.toml && setfacl -m u:1002:r config/authority.toml
make restart-indexer
```

Run it again after an editor replaces the file, since the new file has
no ACL. If `make up` instead reports that `config` is not searchable by
the indexer, run the `setfacl -m u:1002:x` command it prints. If it
reports that another host account has UID 1002, that account could read
the file through the indexer's grant: give it another UID (as root,
`usermod -u <new-uid> <account>`) or remove it.

Do not `chmod 644`/`640` the file or `chgrp` it instead: either lets
other host accounts read your rules. See "Source-authority rules"
in `docs/setup.md`.

## mbsync stopped a sync at its deadline

Each mbsync run has a deadline, `SYNC_DEADLINE_SECONDS` (default 86400, a
day). isync's own 20-second timeout restarts whenever Bridge sends
anything, so a server that keeps a command open with keepalives could
otherwise hold one run, and the whole sync loop, forever (#282). Past the
deadline the entrypoint stops mbsync (TERM, then KILL 30 s later if it is
still running), logs

```text
>>> ERROR: mbsync did not finish within SYNC_DEADLINE_SECONDS=86400 and was stopped; counting this sync as failed (see docs/troubleshooting.md).
```

and counts a failed sync: no success stamp is written, the next attempt
starts after `SYNC_INTERVAL`, and five failures in a row exit the container
so Docker restarts it. A stopped run loses nothing already synced: isync
records each message in its sync state as it goes, so the next run carries
on from there.

**Tune the deadline after your first sync.** The default is deliberately
generous because the first sync of a large mailbox is the longest run
mbsync makes, and its duration is not known in advance. Once the first
sync has finished, find how long it took from the log timestamps:

```bash
docker compose logs -t mbsync | grep -E 'Running initial sync|Starting sync loop'
```

(If the first sync was interrupted or failed, it continued in the next
`>>> Syncing...` runs; add those up.) Later runs only fetch new mail and
take seconds, so a few times the first sync's duration is a safe deadline;
a lower one recovers sooner from a stall. Set it in `.env` and recreate
mbsync:

```bash
# .env: e.g. for a first sync of about 2 hours
SYNC_DEADLINE_SECONDS=21600
```

```bash
make up                # or make up-macos-bridge in macOS Bridge mode
```

Raise it instead if a long catch-up (after mbsync was down for weeks, or a
large import into Proton) keeps being stopped: the log then shows the
deadline line on consecutive runs while Bridge is otherwise working.

## Indexer refuses to start — "wipe the sqlite-volume"

The indexer fails closed when the database was written by an
incompatible schema: one from before the v0 schema renumbering, or one
newer than the running image. The index is derived data, so the fix is
to rebuild it from Maildir. Remove only the index volume; `make clean`
also deletes the Bridge vault and credentials.

Run these from the checkout, with the same project name (`-p` or
`COMPOSE_PROJECT_NAME`) the stack runs under; Compose prefixes volume
names with it, so the volume name is read from the resolved config
rather than assumed:

```bash
volume=$(docker compose config --format json \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["volumes"]["sqlite-volume"]["name"])')
make down
docker volume rm "$volume"
make up                # or make up-macos-bridge in macOS Bridge mode
```

Maildir and Bridge state are untouched. The indexer re-parses and
re-embeds every message, so the rebuild takes as long as an initial
index and calls the embedding provider for the whole mailbox.

## Embedder identity mismatch

The indexer or mcp-server exits at startup with "The configured
embedder is not the one that built this index (...)". The index records
the embedder that built it (see docs/architecture.md, "Embedder
identity record"), and the one configured now differs in the fields
the message lists:

- `EMBED_MODEL`, `endpoint` or `dimensions`: `EMBED_MODEL` or
  `EMBED_BASE_URL` changed (the endpoint is the resolved URL, so an
  empty `EMBED_BASE_URL` reads as `https://api.openai.com/v1`, or as
  `OPENAI_BASE_URL` if set). Both services must use the same values.
- `calibration vector`: the names match but the server behind them
  returns different vectors, typically because a host-side server now
  loads a different model, or a different build of it, under the same
  name, or a provider alias moved.
- `calibration text`: the indexer and mcp-server come from different
  releases, or the index was recorded by another release. Run matching
  images.

Two ways out:

1. Restore the original embedder: put back the `EMBED_BASE_URL` /
   `EMBED_MODEL` values and the model the server loads, then
   `make up`. Nothing is rebuilt.
2. Keep the new embedder and rebuild the index with it, following
   "Indexer refuses to start — wipe the sqlite-volume" above. Vectors
   from two embedders are not comparable, so the old ones cannot be
   kept. Moving the same server to a new address also needs a
   rebuild today; switching embedders without one is PLAN.md Phase 2.

The same section covers "The index holds messages but no record of the
embedder" and "The index predates the embedder identity record": both
mean an index built before the record existed, so rebuild it.

"The indexer has not recorded the embedder behind this index yet" from
mcp-server on a fresh install is transient: the indexer records it once
its embedder answers, and mcp-server's restart picks it up. If it
persists, check `docker compose logs indexer` for an embedder error.

"Embedder calibration request failed" means the startup calibration
request to the embedder failed; the message carries the error type and
status. The service exits and Docker restarts it, so a brief outage
clears by itself; a 401, 403 or 404 is a credential or model setting to
fix (see "Embedder or inference endpoint unreachable from containers").

## Deletion reconciliation (mirror vs archive)

By default the indexer mirrors upstream deletions: a message you delete on
ProtonMail is removed from the local index after a grace window
(`INDEXER_DELETION_GRACE_DAYS`, default 7 days). To keep deleted mail
searchable locally instead (archive mode, an append-only index), set
`INDEXER_DELETION_ENABLED=false` in `.env` and recreate the indexer. Any
value other than true/false (or 1/0, yes/no, on/off) stops the indexer at
startup. A message deleted in Proton usually moves to Trash, and its
Trash copy stays indexed until it is purged there; the MCP tools leave
Trash out of results unless a call names it (`folders=["Trash"]`). A
deleted message that still shows up under Trash (in such a call,
`list_threads(folder="Trash")` or `list_folders`) is expected, not a
reconciliation fault (see *Trash is left out by default* in
`docs/mcp-tools.md`).
See the `Indexer — deletion reconciliation` block in
`.env.example` for all knobs (grace window, sweep interval, mass-delete
brake, unlink-on-reap).

The indexer reads these settings once at startup, so a change takes
effect only when the `indexer` container is recreated. After editing
`.env`, run `make up` (`make up-macos-bridge` in macOS Bridge mode): Compose recreates every container whose
configuration changed. `docker compose restart` is not enough, because
a restarted container keeps the environment it was created with. If
the stack was started with an overlay (such as
`docker-compose.hardened.yml`), run `docker compose up -d` with the
same `-f` files instead, or the recreated container drops the overlay.
To confirm the new value reached the container:

```bash
docker compose exec indexer env | grep '^INDEXER_DELETION_'
```

Defaults — 7-day grace window, 5% mass-delete brake, no file unlink — are
the safe starting point. Quick checks in mirror mode:

```bash
docker compose logs indexer | grep reconciler
```

You should see one line per sweep/reap. If the reaper ever logs
`reaper aborted: ... exceed mass-delete threshold`, investigate why mbsync
marked a large batch as deleted (Bridge vault rebuild, folder rename,
account re-auth) before setting `INDEXER_DELETION_FORCE=true`.

Tombstones and reaper actions can be inspected directly:

```bash
docker run --rm -v protonmail-local-ai_sqlite-volume:/data:ro \
    debian:bookworm-slim bash -c \
    'apt-get -qq install -y sqlite3 >/dev/null && \
     sqlite3 /data/mail.db "SELECT COUNT(*) FROM pending_deletions;"'
```

The reaper sweeps `pending_deletions` on startup and once per
`INDEXER_DELETION_SWEEP_INTERVAL_SECS`. If you want a deletion to land
immediately for testing, drop the grace window to `0` and recreate the
indexer as described above.

## Tuning indexing retries

Every discovered Maildir file is written to an `indexing_jobs` table
and drained by a worker loop. A failure specific to one message
(parser error, SQLite lock contention, input the embedder rejects)
gets exponential backoff and transitions the row to `dead` after
`INDEXER_MAX_ATTEMPTS` attempts. An embedder outage or
misconfiguration (unreachable, rate-limited, bad key or model) is
**not** charged to messages: their jobs are deferred without
spending attempts, and indexing pauses — 30 s, doubling to 10 min —
until the embedder answers again. The indexer logs which case it
hit; a rejected key or model logs an explicit "check EMBED_BASE_URL,
EMBED_MODEL and the embed API key" error.

| Variable | Default | Purpose |
|---|---|---|
| `INDEXER_MAX_ATTEMPTS` | `5` | Max retries before a row becomes `dead`. |
| `INDEXER_RETRY_BASE_SECONDS` | `30` | Base backoff. Each attempt multiplies by `2^(attempts-1)`, capped at 6 h. |
| `INDEXER_MESSAGE_TIMEOUT_SECONDS` | `3600` | Stall guard: the indexer exits (and Compose restarts it) when one message's parse, or one attachment's extraction, runs this long. `0` disables. |

A message that crashes or hangs the indexer is charged one attempt per
restart, retried on its own (in case the whole batch's memory, not the
message, caused the crash), and dead-lettered with
`last_stage = 'interrupted'` once `INDEXER_MAX_ATTEMPTS` is used up; the
logs show a `stall guard:` line for a hang. Other messages in the same
batch are not charged.

`make status` (or the `get_mailbox_status` MCP tool) reports pending,
retrying, and dead counts and whether the index is current. For the
error class breakdown, inspect the table directly:

```bash
docker run --rm -v protonmail-local-ai_sqlite-volume:/data:ro \
    debian:bookworm-slim bash -c \
    'apt-get -qq install -y sqlite3 >/dev/null && \
     sqlite3 /data/mail.db \
       "SELECT status, last_error_class, COUNT(*) FROM indexing_jobs
        GROUP BY status, last_error_class;"'
```

Every failed row records a `last_error_class`:

| Class | Meaning |
|---|---|
| `retryable` | May succeed on a later attempt; `dead` means the attempt budget ran out |
| `permanent_source_failure` | This file can never be indexed under the current config (oversized, no `Message-ID` or one over 998 characters, input the embedder rejects) — dead-lettered immediately |
| `operator_action_required` | The embedder rejected a health probe (bad key or model); jobs stay `queued` until you fix the config |

Once the cause of a dead-letter is fixed, requeue with a fresh budget
while the stack is running:

```bash
make requeue-dead                    # every dead row
make requeue-dead CLASS=retryable    # only exhausted retries
```

The same applies after an upgrade that fixes a parser crash: the
startup scan and periodic recovery skip dead rows, so mail that
dead-lettered on the old version (for example an 8-bit `Date` header
before #361) stays unindexed until you requeue it.

## Claude Desktop doesn't see the tools

1. Verify the MCP server is running: `docker compose ps`
2. Check the server is responding: `curl http://localhost:3000/health`
   should print `{"status":"ok"}`
3. Check the client points at `http://127.0.0.1:3000/mcp`. The legacy
   `/sse` endpoint was removed and now returns `404`; see
   [Connect an MCP client](setup.md#7-connect-an-mcp-client) for the
   Claude Desktop adapter setup
4. Verify the Claude Desktop config JSON is valid (no trailing commas),
   that `command` is `uv`'s absolute path and both paths in `args` are
   absolute, and check `~/Library/Logs/Claude/mcp*.log` for the
   adapter's `ERROR:` line, which names a token-file or `MCP_PORT`
   problem. The adapter logs nothing else, so a server that starts but
   shows no tools means the token was rejected; see the next section
5. Restart Claude Desktop

## MCP client gets 401 Unauthorized

Every request to `/mcp` must send `Authorization: Bearer <token>` with
the token in `.secrets/mcp_auth_token.txt`. The server answers `401`
when the header is missing, uses another scheme, or carries a different
token (including one with extra spaces). `/health` needs no token, so a
healthy container with a `401` from `/mcp` is a client configuration
problem.

1. Check the token works without printing it (from the repository
   root). The header goes to `curl` on standard input (`-H @-`), so the
   token is not in `curl`'s arguments, which other local accounts can
   read. `--noproxy '*'` keeps an `http_proxy` or `ALL_PROXY` in your
   shell from sending the request, header included, to a proxy instead
   of straight to loopback:

   ```bash
   printf 'Authorization: Bearer %s\n' "$(cat .secrets/mcp_auth_token.txt)" |
     curl -s --noproxy '*' -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:3000/mcp \
     -H @- \
     -H 'Accept: application/json, text/event-stream' \
     -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
   ```

   `200` means the server accepts the file's token, so the client is
   sending something else. `401` with the file's token means the server
   started with a different one: the token is read once at startup, so
   run `docker compose restart mcp-server` after changing the file.
2. Claude Code: run `scripts/mcp-auth-headers.sh > /dev/null`; an
   error there names the problem with the token file. Check that the
   `headersHelper` path from
   [Connect an MCP client](setup.md#7-connect-an-mcp-client) is
   absolute, that `claude mcp list` does not say the helper was not run
   (start Claude Code in the repository once and accept the trust
   dialog), and reconnect with `/mcp` in Claude Code.
3. Claude Desktop: the adapter reads `.secrets/mcp_auth_token.txt`
   (or its `--token-file`) only when it starts, so restart Claude
   Desktop after the token changes. To see the adapter's own check of
   the token file, run it once from the repository root with standard
   input closed: `uv run --directory mcp-server --frozen python -m
   src.stdio_adapter < /dev/null`. An `ERROR:` line names the problem;
   no output means the file passed.
4. Codex: run `scripts/mcp-auth-headers.sh > /dev/null` as for Claude
   Code, and check that `http_headers_helper` in `~/.codex/config.toml`
   is the script's absolute path in single quotes (Codex runs it with
   `sh -c`). With `bearer_token_env_var` instead,
   check that the variable is exported in the shell that starts Codex.

The server logs a request with a wrong token as `Auth error returned:
invalid_token (status=401)` and never logs the token or the
`Authorization` header.

## mcp-server exits with "The MCP bearer token is missing or empty"

`.secrets/mcp_auth_token.txt` is empty (or holds only whitespace).
`make validate-env`, which `make up` runs first, catches this too.
Create a token with `make init-secrets` (when the file does not exist)
or `(umask 077; openssl rand -hex 32 > .secrets/mcp_auth_token.txt)`,
run `make up` (`make up-macos-bridge` in macOS Bridge mode), and
configure each client with it.

## mcp-server exits with "The MCP bearer token must be at least 32 characters"

The token in `.secrets/mcp_auth_token.txt` is shorter than 32
characters or holds a character outside `A-Z a-z 0-9 - . _ ~ + /` (with
`=` allowed only at the end), so MCP clients could not send it as a
bearer token. `make validate-env` reports the same. Replace it with
`(umask 077; openssl rand -hex 32 > .secrets/mcp_auth_token.txt)`, run
`make up`, and update each client.

## mcp-server exits with "MCP_TRANSPORT=sse was removed"

The `.env` (or the shell you run `make` from) still sets
`MCP_TRANSPORT=sse` or `MCP_TRANSPORT=dual` from a release that served
the legacy SSE transport. Remove the line from `.env`, run
`unset MCP_TRANSPORT` in a shell that exports it (an exported value wins
over `.env`), or set it to `streamable-http`; then run `make up`
(`make up-macos-bridge` in macOS Bridge mode) and change client URLs from `/sse` to `/mcp`.

## Bridge credentials expired / need to re-authenticate

```bash
make down
docker volume rm protonmail-local-ai_bridge-data
make first-run   # log in again
```

After login, copy the new `Username` into `.env` and write the new `Password`
into `.secrets/bridge_pass.txt`:

```bash
printf '%s' 'new-bridge-generated-pass' > .secrets/bridge_pass.txt
chmod 600 .secrets/bridge_pass.txt
make up
```

Your email index is in a separate volume (`sqlite-volume`) and is not affected.

The new vault comes with a new Bridge TLS cert, but the `mbsync-state`
volume still holds the pin for the old one, so `mbsync` now refuses to
sync with `Bridge cert fingerprint does not match pinned value`. That
is the pin working as intended. Once `make logs` confirms the mismatch
is the one this re-authentication caused, accept the new cert with the
two-step rotation in
[mbsync refuses to sync — Bridge cert pin mismatch](#mbsync-refuses-to-sync--bridge-cert-pin-mismatch):
recreate `mbsync` once with `BRIDGE_CERT_PIN_ROTATE=true`, check the
`rotating pin` warning, then recreate it with
`BRIDGE_CERT_PIN_ROTATE=false` to re-enable pin enforcement.

Once the pin is rotated, the new Bridge may number the mailbox's
messages differently. If mbsync then reports `UIDVALIDITY genuinely
changed`, see
[mbsync reports a UIDVALIDITY change](#mbsync-reports-a-uidvalidity-change).

## mbsync refuses to sync — Bridge cert pin mismatch

On first boot `mbsync` extracts Bridge's TLS cert, computes its SHA-256
fingerprint, and saves it to a persistent state volume (`mbsync-state`).
On every subsequent boot the freshly extracted cert is compared to the
pinned fingerprint. A mismatch is treated as a security event and
`mbsync` refuses to sync. Log output looks like:

```
>>> ERROR: Bridge cert fingerprint does not match pinned value — refusing to sync.
>>>   pinned:  sha256:<old>
>>>   current: sha256:<new>
```

Legitimate cert rotations happen when Bridge is upgraded or `vault.enc`
is regenerated. To accept the new cert, recreate `mbsync` once with
`BRIDGE_CERT_PIN_ROTATE=true`:

```bash
BRIDGE_CERT_PIN_ROTATE=true docker compose up -d mbsync
```

The container writes the new fingerprint to the pin file on startup and
syncing resumes. Check the `rotating pin` warning in `make logs` and
that its `current:` fingerprint is the one you expect.

The flag is not consumed by the rotation. It is part of the container's
environment, so it stays `true` through every later restart of that
container — `docker compose restart`, or Docker's restart policy after
a crash — and changing your shell or `.env` does not update a container
that already exists. While it stays `true`, every cert change is
accepted without comparison. As soon as the rotation has succeeded,
recreate `mbsync` with rotation disabled:

```bash
BRIDGE_CERT_PIN_ROTATE=false docker compose up -d --no-deps --force-recreate mbsync
```

The startup log no longer shows the `BRIDGE_CERT_PIN_ROTATE=true`
warning once enforcement is back. Also make sure `.env` does not set
`BRIDGE_CERT_PIN_ROTATE=true`, or the next `make up` re-enables it. If
the stack was started with an overlay (such as
`docker-compose.hardened.yml`), pass the same `-f` files to both
commands, or the recreated container drops the overlay.

If the pin cannot be saved — first boot or rotation, for example because
the `mbsync-state` volume is full or not writable — `mbsync` refuses to
sync rather than trust a cert it could not pin, and a failed rotation
keeps the previous pin:

```
>>> ERROR: could not save the Bridge cert pin to /state/bridge-cert.fingerprint — refusing to sync.
```

Fix the volume and restart `mbsync`. After a successful rotation,
recreate it with `BRIDGE_CERT_PIN_ROTATE=false` as above; a restart
alone keeps rotation enabled, and leaving it enabled disables pin
enforcement.

Only a missing pin file is treated as a first boot. A pin file that
exists but is empty, malformed, unreadable or a dangling link is
damaged state: `mbsync` refuses to sync and leaves it in place rather
than silently re-pinning whatever cert Bridge presents:

```
>>> ERROR: the Bridge cert pin at /state/bridge-cert.fingerprint is empty or malformed — refusing to sync.
```

(An unreadable pin or dangling link logs `could not read the Bridge
cert pin` instead.) Find out how the pin was damaged first, then accept
the cert Bridge presents now with the same one-time
`BRIDGE_CERT_PIN_ROTATE=true` run as for a rotation; it replaces an
empty, malformed or unreadable pin file and a dangling link.

A directory, FIFO or device in the pin's place is refused before it is
read, with or without rotation:

```
>>> ERROR: the Bridge cert pin at /state/bridge-cert.fingerprint is not a regular file — refusing to sync.
```

Remove it from the `mbsync-state` volume by hand, for example with
`docker compose run --rm --no-deps --entrypoint rm mbsync -r
/state/bridge-cert.fingerprint` (pass the same `-f` overlay files as
for `up`). The next start is then a first boot and pins whatever cert
Bridge presents, as a rotation would.

`make clean` removes the `mbsync-state` volume along with everything
else, so the next boot after `make clean` is treated as a first boot
and trust-on-first-use re-pins whatever cert Bridge presents.

`make clean` also truncates `.secrets/bridge_pass.txt` because it
authenticates against Bridge state that the volume wipe just deleted
(`vault.enc`). After `make clean` you must re-run `make first-run`
and paste the new Bridge password into `.secrets/bridge_pass.txt`.
Inference / embed / rerank provider keys
(`.secrets/inference_api_key.txt`, `.secrets/embed_api_key.txt`,
`.secrets/rerank_api_key.txt`) are intentionally preserved because
they authenticate against external services that survive container
rebuilds.

## mbsync fails — TLS hostname mismatch after rebuilding Bridge image

The TLS cert Bridge generates is cached inside `vault.enc` in the `bridge-data`
volume. Rebuilding the image does not regenerate the cert — the old one
(issued for `127.0.0.1` only) is reused. To force a fresh cert with the correct
SANs, delete `vault.enc` from the volume without wiping the GPG/pass store:

```bash
make down
docker run --rm -v protonmail-local-ai_bridge-data:/data debian:bookworm-slim \
    rm -f /data/config/protonmail/bridge-v3/vault.enc
make first-run   # re-login; Bridge generates a new cert with protonmail-bridge SAN
make up
```

Deleting `vault.enc` is a full re-authentication path, not a lightweight cert
refresh. Plan on logging into Bridge again, updating `BRIDGE_USER` and
`.secrets/bridge_pass.txt` before `make up`, and rotating the mbsync cert
pin afterwards: the new cert does not match the pin in `mbsync-state`, so
`mbsync` refuses to sync until you complete the two-step rotation in
[mbsync refuses to sync — Bridge cert pin mismatch](#mbsync-refuses-to-sync--bridge-cert-pin-mismatch).
