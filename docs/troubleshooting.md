# Troubleshooting

Diagnostics and recovery steps for a running stack. For first-time
installation and configuration, see [`setup.md`](setup.md).

## Which layer is failing

A Bridge app that accepts TCP connections on its IMAP port does not
mean TLS works, an account is logged in, or mail is syncing. Each layer has its own signal; the states and signals
are defined in
[architecture.md](architecture.md#health-and-readiness-signals). Start
with `make status`, which shows container health, each provider layer
as LOCAL or REMOTE (host name only) with the no-egress overlay state,
and the `get_mailbox_status` fields, then find the symptom below. Only the
authentication row involves a credential: the Bridge app's IMAP details
show the Bridge password, so treat them and `.secrets/bridge_pass.txt`
as secrets. No other check needs or shows one.

| Symptom | Failing layer | Next safe check |
| --- | --- | --- |
| mbsync logs `Waiting for ProtonBridge IMAP` with no `Bridge IMAP port is reachable` after it, then `Bridge IMAP did not become reachable` | Bridge listening, as mbsync sees it | [The app is running and its port matches](#mbsync-cannot-reach-or-verify-the-bridge-app) |
| mbsync stops with `cert extraction failed` | TLS handshake | Run the TLS probe in [Confirm Bridge IMAP answers over TLS](#confirm-bridge-imap-answers-over-tls); [the app's IMAP mode must be SSL](#mbsync-cannot-reach-or-verify-the-bridge-app) |
| mbsync stops with `Bridge cert fingerprint does not match pinned value` or `does not match BRIDGE_CERT_FINGERPRINT` | TLS identity | [Bridge cert pin mismatch](#mbsync-refuses-to-sync--bridge-cert-pin-mismatch) and [the app's certificate](#mbsync-cannot-reach-or-verify-the-bridge-app) |
| mbsync is healthy, logs `Initial sync returned a non-zero status` or `Sync failed (n/5 consecutive failures)`, restarts after five; `get_mailbox_status` says `no successful mail sync has been recorded` | Bridge authentication, or the sync itself | Check that `BRIDGE_USER` and `.secrets/bridge_pass.txt` match the Bridge app's IMAP details and that an account is signed in to the app ([re-authenticate](#bridge-credentials-expired--need-to-re-authenticate)); `mbsync -V` in [Verifying mbsync is working](#verifying-mbsync-is-working) names the failing step |
| `make up` fails with `container mbsync is unhealthy` | mbsync syncing | [`make up` fails — mbsync is unhealthy](#make-up-fails--mbsync-is-unhealthy) |
| mbsync is healthy, `get_mailbox_status` says `last successful mail sync was ... ago` | Last successful sync (recent syncs failing, or one long run in progress); or the indexer has not acknowledged a newer stamp (see the indexer rows) | `docker compose logs mbsync --tail 50`: repeated `Sync failed` lines, or no `Syncing...` since a long run started ([deadline](#mbsync-stopped-a-sync-at-its-deadline)) |
| `get_mailbox_status` says `the indexer has not reported` or `the indexer last reported ... ago` | Index current: indexer down or stalled | `docker compose logs indexer --tail 50` and `docker inspect indexer --format='{{json .State.Health}}'` |
| `get_mailbox_status` says `... waiting to be indexed` | Index current: indexing behind, or a reparse after an upgrade (the reason then adds `... already indexed and being reparsed`) | Normal after a large sync or an upgrade; if it does not fall, see [Tuning indexing retries](#tuning-indexing-retries) and [Reparse or rebuild after an upgrade](#reparse-or-rebuild-after-an-upgrade) |
| `get_mailbox_status` is current but a message is missing | Index: a dead-lettered message (`current` ignores the `dead` count), or none: the mail reached Proton after the last sync | If the `dead` count is non-zero, fix its cause and run `make requeue-dead` (see [Tuning indexing retries](#tuning-indexing-retries)); otherwise wait one `SYNC_INTERVAL` |

## Which build and settings is a container running?

Ask the MCP client "What version of the mail tools are you running?"
`get_mailbox_status` returns `server_version` in both its text and structured
output. This is the MCP server image's source commit, including `-dirty`
for local changes, or `unknown` when build identity is unavailable. The
same value is advertised as the MCP server's version over Streamable HTTP.
Claude Desktop's stdio adapter advertises its own FastMCP version instead;
use the tool's `server_version` to identify the deployed server through it.
It does not report the indexer or mbsync build, or the database schema version.

Each service logs one `Startup identity` line when it starts, before it
parses or checks any setting, so the line is there even when startup
then fails (a malformed setting, a missing token, a refused index or a
failed migration):

```text
indexer     ... Startup identity: service=indexer commit=1a2b3c4 boot=5f0e9d8c7b6a schema_code=1 schema_stored=1 config=0123456789ab
mcp-server  ... Startup identity: service=mcp-server commit=1a2b3c4 boot=0a1b2c3d4e5f schema_stored=1 config=ba9876543210
mbsync      >>> Startup identity: service=mbsync commit=1a2b3c4 boot=9e8d7c6b5a4f config=c0ffee123456
```

Find it with `docker compose logs <service> | grep 'Startup identity'`.

- `commit` is the checkout the image was built from, passed by
  `make build`; `-dirty` means a tracked file was modified or an
  untracked file was present that Git does not ignore (`.env` and
  `.secrets/` are ignored, so they never count). A `GIT_COMMIT` set in
  the shell is ignored; `make build GIT_COMMIT_OVERRIDE=<value>` labels
  a build explicitly. `unknown`
  means the image was built another way (a plain `docker compose build`,
  or by `make up` when no image existed): run `make build`. The same
  value is on the image:
  `docker image inspect <image> --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'`.
- `boot` is random for every process start. Lines between one `boot`
  and the next come from the same run, so a restart shows up as a new
  value.
- `schema_code` (indexer only) is the schema version the code expects.
  `schema_stored` is the version stamped in the index file, read
  read-only before the service opens it (and, for the indexer, before
  any migration): `none` when there is no index yet, `unreadable` when
  SQLite cannot read the file. A `schema_stored` above `schema_code` is
  a downgrade, which the indexer refuses; one below it means
  migrations run on this start.
- `config` is the first 12 hex digits of a SHA-256 over the service's
  non-secret settings as configured: the raw environment values, not
  parsed, so a malformed value just gives a different hash, and an
  unset setting hashes differently from one set to its default. A
  `*_BASE_URL` (or an mbsync host or port) that carries credentials
  (`user:password@`) is hashed as a fixed marker, so the hash cannot be
  used to test guesses at the password. The inputs are named one by one in code: every non-secret setting the
  service reads, that is paths (including the indexer's health file),
  modes, endpoints, model names, the MCP transport and limits (indexer
  and mcp-server, `_IDENTITY_SETTINGS` in `src/main.py`, with everything
  else the service reads listed with its reason in
  `_IDENTITY_EXCLUDED`; a test fails when a new setting is in neither),
  or `BRIDGE_HOST`, `BRIDGE_IMAP_PORT`,
  `BRIDGE_CERT_HOST`, `SYNC_INTERVAL`, `SYNC_DEADLINE_SECONDS`,
  `BRIDGE_CERT_FINGERPRINT` (normalized, and not secret) and
  `BRIDGE_CERT_PIN_ROTATE` (mbsync). It changes when one of those
  settings changes and is otherwise stable across restarts. API keys,
  the MCP bearer token, the Bridge user and the Bridge password are not
  inputs, so rotating a secret leaves it unchanged.

## Bridge is up but IMAP is unresponsive / mbsync can't connect

Bridge may still be in the middle of its initial sync — pulling every
message from Proton's API into its local database before it can serve
IMAP. This is not the same as mbsync syncing to Maildir. It happens
inside the Bridge app and can take hours on a large mailbox; the app
shows its progress. IMAP can be unresponsive while it runs, and
`mbsync` fails closed instead of syncing without a pinned Bridge cert:
after a bounded wait it exits and Docker restarts it, so the failure is
visible instead of hanging forever. mbsync extracts the Bridge TLS
cert itself on its next start; then check it with "Verifying mbsync is
working" below.

Whether an account is logged in is shown in the app. Bridge listens,
completes TLS and greets IMAP clients with no account logged in, so the
signal on this side is mbsync: a sync that completes
(`get_mailbox_status` reports a last sync) proves its login was
accepted, while a rejected login shows as repeated `Sync failed` lines
in `docker compose logs mbsync`.

### Confirm Bridge IMAP answers over TLS

Bridge serves IMAP with implicit TLS (#638), so a plaintext client such as
`nc` gets no greeting from it. Probe from a terminal on the host, where
the app listens on the loopback interface (replace 1143 with the app's
IMAP port if it differs); no credential is sent:

```bash
printf 'a1 LOGOUT\r\n' | timeout 20 openssl s_client -connect 127.0.0.1:1143 -quiet -ign_eof
```

The `verify error:num=18:self-signed certificate` line is expected: this
probe only checks that TLS works and IMAP answers, and trusts nothing (mbsync
checks the certificate against `BRIDGE_CERT_FINGERPRINT` and its pin). If
IMAP is ready, the TLS lines are followed by the Bridge greeting and the
logout, e.g.:

```
* OK [CAPABILITY AUTH=PLAIN ... IMAP4rev1 ...] Proton Mail Bridge 03.27.00 - gluon session ID 1
* BYE
a1 OK LOGOUT
```

A greeting shows the listener and TLS work, not that an account is logged
in: Bridge greets the same way with no account. A handshake error
(usually `wrong version number`) means the app's IMAP connection mode is
STARTTLS rather than SSL; switch it to SSL. A probe that hangs until
`timeout` stops it means Bridge is still in its initial sync or has
stopped responding — check the app.

## Verifying mbsync is working

Run these checks in order of depth.

**1. Is mbsync running and looping?**

```bash
docker compose logs mbsync --tail 20
```

Look for `>>> Syncing...` lines repeating at your `SYNC_INTERVAL`, each
followed by `>>> Sync ok in <n>s (deadline <n>s)` when the sync succeeds. If
the permission repair after a sync fails, mbsync logs
`find/chmod reported <n> error line(s)` with a `docker exec` command that
lists the entries still unreadable; the errors themselves are not logged,
because their paths name folders. If startup
fails, `mbsync` now logs a specific cause such as:

- missing `BRIDGE_USER`
- missing or empty `/run/secrets/bridge_pass`
- cert extraction timeout
- `openssl s_client` handshake errors. mbsync connects with implicit
  TLS only (#638), so the app's IMAP connection mode must be SSL; see
  [mbsync cannot reach or verify the Bridge app](#mbsync-cannot-reach-or-verify-the-bridge-app)
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

Bridge takes a few seconds to start listening. mbsync waits automatically, but it
now gives up after a bounded wait and lets Docker restart it rather than
appearing healthy forever. If it keeps failing, check that the Bridge app
is running and signed in (see the next section), and:

```bash
docker compose logs mbsync
```

If you want Docker's view of the current state:

```bash
docker inspect mbsync --format='{{json .State.Health}}'
```

## mbsync cannot reach or verify the Bridge app

Bridge is the Proton Mail Bridge app on the host
([setup](setup.md#4-set-up-the-proton-mail-bridge-app)); check the app
and the connection.

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
  ([setup](setup.md#4-set-up-the-proton-mail-bridge-app), step 4.3; it
  does not change with the mode), and `make up`.
- **`BRIDGE_CERT_FINGERPRINT is not set`.** mbsync does not trust
  the app's certificate on first use, so it stops at startup, before it
  waits for or connects to the app (`make validate-env`, and so
  `make up`, refuse first). Take the fingerprint on the host and set it
  in `.env` ([setup](setup.md#4-set-up-the-proton-mail-bridge-app),
  step 4.3), then `make up`.
- **`the Bridge certificate does not match BRIDGE_CERT_FINGERPRINT`.**
  Compare the `presented:` value with the fingerprint taken on the host
  while the app is running. If they differ, something other than the app
  answered on its port (for example another local account's process
  while the app was closed): treat it as a security event. If the app's
  certificate changed on purpose (reinstall, reset), update
  `BRIDGE_CERT_FINGERPRINT` and rotate the pin as in
  [Switching to another Bridge installation](setup.md#switching-to-another-bridge-installation).
- **`certificate owner does not match hostname host.docker.internal`.**
  mbsync runs with `BRIDGE_CERT_HOST` changed from `127.0.0.1` (a
  modified `docker-compose.yml` or an overlay). Restore it and
  `make up`.
- **`certificate owner does not match hostname 127.0.0.1`.** The app
  presents a certificate not issued for `127.0.0.1`, such as one
  imported into Bridge by hand. Use a certificate for `127.0.0.1`; there
  is no option to skip the check.
- **`Bridge cert fingerprint does not match pinned value` after
  reinstalling the app or resetting it.** Expected once: the app
  presents a different certificate. Verify it and rotate the pin as in
  [Switching to another Bridge installation](setup.md#switching-to-another-bridge-installation).
- **`UIDVALIDITY genuinely changed` or `Unable to recover from
  UIDVALIDITY change` after switching Bridge installations.** mbsync's sync state
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
line isync 1.5.1 prints, `Error: channel protonmail: far side box <name>
cannot be opened anymore.`, or the one isync 1.4.4 printed, without
`anymore`; a different isync version that words it differently falls back
to counting the run as a failure.

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
`docker logs mbsync` (#570). Every isync 1.5.1 message that names a folder
(and every 1.4.4 one) is logged with the name replaced and the rest of the
message kept:

- a folder name becomes `<folder>`, for example
  `Error: channel protonmail, far side box <folder> (at UID 42): UIDVALIDITY genuinely changed.`
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
  `IMAP command 'UID FETCH 1:5 (UID FLAGS)' returned an error: (server text withheld)`.

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

## A `Starred` folder is left from an earlier sync

Proton's `Starred` is a virtual folder: a second copy of each starred
message. mbsync no longer syncs it (#692), but it never deletes local
mail, so a Maildir synced before that change keeps its `/maildir/Starred`
directory, and the indexer indexes every folder it finds there. The
starred messages then appear twice: `query_messages` counts both copies,
and `get_mailbox_status` lists them under `conflicting_message_ids`.

After updating mbsync (`make build`, `make up`), remove the leftover
directory once. Only the top-level `Starred` is virtual; a custom folder
is `/maildir/Folders/.Starred` and is not touched:

```bash
docker exec mbsync rm -rf /maildir/Starred
```

The real copies, and their starred flag, are unaffected. If the index
already held the duplicates, deletion reconciliation removes them after
its grace period (`INDEXER_DELETION_GRACE_DAYS`); in archive mode
(`INDEXER_DELETION_ENABLED=false`) they stay until the index is rebuilt.

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
make up
```

To keep a copy of the old Maildir first, back it up as in
[Recover from a genuine change](#recover-from-a-genuine-change).
The Bridge app and mbsync's certificate pin (the `mbsync-state` volume)
are untouched. mbsync then pulls the whole mailbox, and the indexer
rebuilds the index from it, which re-embeds every message (a cost with a
paid embedding provider).

## mbsync reports a UIDVALIDITY change

Reinstalling or resetting Bridge, re-adding the account, or switching
to another Bridge installation can leave three pieces of mbsync's state
stale. Each
one stops the sync before the next can show, so after such a change they
appear in this order:

| Log line in `docker logs mbsync` | What is stale | Fix |
| --- | --- | --- |
| `the Bridge certificate does not match BRIDGE_CERT_FINGERPRINT`, or `Bridge cert fingerprint does not match pinned value` | The expected fingerprint and the certificate pin. Checked before mbsync logs in. | Verify the new certificate, update `BRIDGE_CERT_FINGERPRINT`, then rotate the pin: [Bridge cert pin mismatch](#mbsync-refuses-to-sync--bridge-cert-pin-mismatch) and [the Bridge app](#mbsync-cannot-reach-or-verify-the-bridge-app). |
| `IMAP command 'LOGIN <user> <pass>' returned an error: (server text withheld)` (or `AUTHENTICATE PLAIN <authdata>`) | The credentials: Bridge refused `BRIDGE_USER` or `.secrets/bridge_pass.txt`. | Copy the new username and password from Bridge: [re-authenticate](#bridge-credentials-expired--need-to-re-authenticate), or step 2 of [Switching to another Bridge installation](setup.md#switching-to-another-bridge-installation). |
| `Error: channel protonmail, far side box <folder> (at UID 42): UIDVALIDITY genuinely changed.` or `... Unable to recover from UIDVALIDITY change.` | The sync state: this Bridge numbers the folder's messages differently. | Below. |

### Spurious or genuine

mbsync records each folder's IMAP UIDVALIDITY and the UID of every
message it pulled, in the folder's `.mbsyncstate` (see
[Folder names in mbsync's log](#folder-names-in-mbsyncs-log)). When
Bridge reports a different UIDVALIDITY, isync 1.5.1 tells the two cases
apart itself, by comparing the Message-ID of each message it pulled with
the message Bridge now serves at that UID:

- **Spurious**: every message it can check is still at its old UID.
  isync accepts the new UIDVALIDITY, logs
  `Notice: channel protonmail, far side box <folder>: Recovered from change of UIDVALIDITY.`
  and syncs as usual. Nothing to do.
- **Genuine**: a UID now holds another message. isync logs
  `(at UID <n>): UIDVALIDITY genuinely changed`.
- **Unknown**: no UID contradicts the old state, but too few messages
  could be confirmed (fewer than 20, and fewer than 80% of those it
  pulled before; typical of Drafts). isync logs
  `Unable to recover from UIDVALIDITY change`. Treat it as genuine.

A `near side box` line means the local Maildir's own UIDVALIDITY
changed, which happens when something other than mbsync rewrites the
Maildir (a restore from a backup, for example). isync checks that side
the same way; when both sides changed and either change is genuine, it
logs `Unable to recover from both-sided UIDVALIDITY change, as it is
genuine on at least one side`. Treat a near-side or both-sided error as
genuine too.

In each error case isync skips that folder and changes nothing in it
(mbsync is pull-only and never expunges); the other folders keep
syncing. The run counts as a failed sync, and five in a row restart
mbsync. Retrying does not help: the state cannot be reused.

If every folder reports a genuine change right after `BRIDGE_USER`
changed, first check that it names the right account (the account's
IMAP details in the Bridge app). Another
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
make up
docker logs mbsync     # no UIDVALIDITY errors
```

What this keeps and changes:

- The archive is the old Maildir as it was, every `.eml` included, and
  is the only copy of mail that Proton no longer has (deleted there but
  kept locally, with the `T` flag). It is your mailbox, unencrypted:
  keep it outside the checkout, as above, so no `git add` can pick it
  up, and on an encrypted disk (FileVault).
- The sync state lives inside the Maildir, so it goes with it. The
  certificate pin (the `mbsync-state` volume) and the Bridge app are
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
Once mbsync is healthy, run `make up` again: Compose leaves the running
services as they are and starts the indexer and then the MCP server.

## Indexer cannot read `config/authority.toml` on Linux

On Linux with Docker Engine the indexer (UID 1002) sees the host owner
and mode of the bind-mounted `config/authority.toml`, so a `600` file
you own stops it at startup with "could not be read". `make up` and
`make restart-indexer` catch this first and print the fix, which grants
UID 1002 alone read access with an ACL (needs the `acl` package):

```bash
setfacl -b config/authority.toml && chmod 600 config/authority.toml && setfacl -m u:1002:r config/authority.toml
```

Then rerun the command that failed: `make up` on first run,
since no indexer container exists yet for a restart to act on, or
`make restart-indexer` for a running stack.

Run the `setfacl` line again after an editor replaces the file, since the new file has
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
sync has finished, find how long it took: each successful sync logs
`>>> Sync ok in <n>s (deadline <n>s)`.

```bash
docker compose logs mbsync | grep 'Sync ok'
```

(If the first sync was interrupted or failed, it logged no such line and
continued in the next `>>> Syncing...` runs; add up their durations from
the log timestamps, `docker compose logs -t mbsync`.) Later runs only fetch new mail and
take seconds, so a few times the first sync's duration is a safe deadline;
a lower one recovers sooner from a stall. Set it in `.env` and recreate
mbsync:

```bash
# .env: e.g. for a first sync of about 2 hours
SYNC_DEADLINE_SECONDS=21600
```

```bash
make up
```

Raise it instead if a long catch-up (after mbsync was down for weeks, or a
large import into Proton) keeps being stopped: the log then shows the
deadline line on consecutive runs while Bridge is otherwise working.

## Indexer refuses to start — "wipe the sqlite-volume"

The indexer fails closed when the database was written by an
incompatible schema: one from before the v0 schema renumbering, or one
newer than the running image. The index is derived data, so the fix is
to rebuild it from Maildir. Remove only the index volume; `make clean`
also deletes the Maildir and mbsync's certificate pin.

Run these from the checkout, with the same project name (`-p` or
`COMPOSE_PROJECT_NAME`) the stack runs under; Compose prefixes volume
names with it, so the volume name is read from the resolved config
rather than assumed:

```bash
volume=$(docker compose config --format json \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["volumes"]["sqlite-volume"]["name"])')
make down
docker volume rm "$volume"
make up
```

Maildir and Bridge state are untouched. The indexer re-parses and
re-embeds every message, so the rebuild takes as long as an initial
index and calls the embedding provider for the whole mailbox.

## Indexer stops during a schema migration

An index from an older release is migrated at startup: the log shows
`Migrating database at ...: vN -> vM`, then `Applying migration ...`
and `Database ready at ... (schema vM, applied migrations: [...])`.
Each migration runs in one transaction, so if the indexer stops part
way (killed, disk full) the index stays at the last version that
finished, and the next `make up` retries the rest; nothing needs to be
deleted. If a retry keeps failing with the same error, rebuild the
index as in the section above. If a migration finished but the
result is wrong, recreate the containers from the release before it
and then restore the copy taken before the upgrade, in that order
([Back up and restore the index](#back-up-and-restore-the-index)). v1 (#928) keys the attachment
extraction cache by extractor module; after it, an attachment whose
label selects a different extractor from the one that first read its
bytes is re-extracted once, the next time its message is reprocessed.

## Reparse or rebuild after an upgrade

Some releases change what the parser stores for each message. Which
kind of reindex that needs depends on whether search data changes too:

- **Reparse** (#1078): the change adds per-message data (a column, a
  table) but changes no chunk, chunk text, embedding input, Message-ID
  or threading. Its migration queues every indexed message with reason
  `reparse` at startup; the indexer re-reads each file and rewrites its
  per-message rows, with no embedding calls (attachment text comes from
  the extraction cache). Search keeps working throughout; the data the
  release adds is missing for a message until its reparse runs. New
  mail, recovery and re-extraction jobs go ahead of the reparse, which
  still advances at least one message per batch, so the queue
  heartbeat's `oldest_due_age` grows while it runs without meaning
  draining has stalled. Progress is in the log every five
  minutes (`reparse: remaining=... reparsed_since_last_heartbeat=...
  dead=...`), then one `reparse complete: ...` line; `make status`
  shows the remaining count (`queue.reparse`). A message that fails to
  parse dead-letters like any other; fix the cause, then
  `make requeue-dead`.
- **Rebuild**: the change alters chunks or what is embedded (for
  example a new chunk ID derivation or chunker), or the embedder
  model. Every message must be embedded again; rebuild from Maildir as
  in [Indexer refuses to start](#indexer-refuses-to-start--wipe-the-sqlite-volume).

The release notes and the migration's header say which applies.
`make reparse` queues the same reparse by hand, for recovery (for
example when a reparse's jobs were cleared another way). Messages that
already have a job keep it, and dead-lettered ones stay dead until
`make requeue-dead`. It runs inside the indexer container, so the
stack must be up:

```bash
make reparse
```

## Back up and restore the index

Take a copy of the index before deploying a release that changes the
schema (a new `indexer/src/migrations/` file), so a migration that
commits but turns out wrong can be undone without a full rebuild from
Maildir (#1005). The copy holds the whole mailbox: choose a directory
outside the checkout, on an encrypted disk, and delete copies you no
longer need (docs/architecture.md, "Backups").

```bash
make backup-index BACKUP_DIR="$HOME/protonmail-local-ai-backup"
```

The stack keeps running. The indexer container copies the index with
SQLite's online backup API from a read-only connection, which reads one
consistent snapshot while the indexer writes, into a temporary file next
to the index in the index volume (so the volume needs free space for one
more copy of `mail.db` while it runs). It runs `PRAGMA integrity_check`
on that copy, streams it to `BACKUP_DIR/mail-<UTC timestamp>-<run>.db`
(where `<run>` is the process ID and four random bytes, so two backups
started in the same second never share a name, in the volume or on the
host), checks the SHA-256 of the host file against the container's, and
removes the temporary file. A temporary copy left by a run that was killed before
its cleanup (`.backup-index-*.db` in the volume), and the staging file a
killed restore leaves (`.restore-index.db`, see below), are removed by
the next backup once they are more than 6 hours old (a run or a restore
still writing keeps its file), and the run says how many of each it
removed. The target prints the integrity result, the path, the
size and the schema version, and writes nothing to `BACKUP_DIR` when
the check fails or when the copy lacks what `restore-index` requires
(this project's application ID and a schema version, which an index the
indexer is still creating may not have yet). `BACKUP_DIR` is required; it is created with mode 700,
and the directory (new or existing) and the file are refused when mode
bits or an ACL give other users access (on macOS, `chmod -N <dir>`
removes ACL entries); a path inside the checkout is refused, and the
file is mode 600. No mount or setting of
the running containers changes.

To go back to a copy:

```bash
make restore-index BACKUP="$HOME/protonmail-local-ai-backup/mail-20261007T120000Z-48213-9f3ac1d2.db"
```

It asks for `yes`, then:

1. stops `mcp-server` and `indexer` (the stack must have been started
   with `make up`, so the containers exist; mbsync keeps running);
2. streams the file into a one-off container of the indexer's image with
   the index volume, the indexer's user, a read-only root, no
   capabilities and no network, which refuses the file unless
   `PRAGMA integrity_check` is `ok`, the file is this project's index
   and its schema version is not above the code's;
3. checkpoints the current index's WAL into `mail.db`, so that file
   alone holds every committed change if the swap fails, then removes
   the `-wal` and `-shm` files and renames the copy over `mail.db` with
   the current file's mode, so mcp-server (another user) can still read
   it (an old WAL left beside the restored file would be replayed into
   it); a staged copy (`.restore-index.db` in the volume) is removed on
   any failure the container handles, a full volume included, and one
   left by a killed container is removed by the next `make backup-index`
   once it is 6 hours old;
4. starts `indexer` and waits up to `RESTORE_WAIT_SECONDS` (900, checked
   before anything is stopped) for the startup lines of that new
   process, printing the `Startup identity` line
   (`schema_stored`), any `Migrating database` and `Database ready`
   lines and the `Embedder identity verified` line;
5. starts `mcp-server` only after that line, so it never serves an
   index the indexer is still migrating. If the indexer refuses the
   restored index or does not report in time, `mcp-server` is left
   stopped: fix the cause and run `make up`. When step 2 or 3 refuses
   the file, the index is unchanged and both services start again.

If the current index cannot be opened at all, step 3 refuses; remove
the index volume as in
[Indexer refuses to start](#indexer-refuses-to-start--wipe-the-sqlite-volume),
run `make up` so the containers exist, then restore.

The current index is replaced, so take a `make backup-index` first if
you may want it back. Mail that arrived after the copy was taken is
indexed again when the indexer starts, since it queues every Maildir
message the index does not hold. The indexer refuses a copy made with
a different embedder; see
[Embedder identity mismatch](#embedder-identity-mismatch).

The restore runs, and then starts, the image of the existing indexer
container, and that indexer migrates an older copy again on its first
start. To undo a migration that finished but is wrong, recreate the
containers from the release before it first, then restore:

```bash
# The release before schema v1 has no restore helper: keep this one.
cp scripts/restore-index.sh "$HOME/restore-index.sh"
git checkout <the release before the migration>
make build
# Only these two (--no-deps), so mbsync keeps running; add the same
# -f overlays you run with.
docker compose up --no-start --no-deps indexer mcp-server
BACKUP="$HOME/protonmail-local-ai-backup/mail-<before the upgrade>.db" \
  bash "$HOME/restore-index.sh"
rm "$HOME/restore-index.sh"
```

`up --no-start --no-deps` replaces the indexer and mcp-server
containers with ones built from that release without starting them, so
nothing opens the migrated index, and leaves mbsync running;
`restore-index.sh` then installs the copy and starts the two. The
helper needs nothing from the checkout, so the copy taken before the
checkout works with the older release (its own `make restore-index`
target is the same script where the release has one).

If the one-off restore container (`restore-index-<pid>`) is killed or
Docker fails part way, the script cannot tell whether the copy was
swapped in. A failed `docker run` does not prove the container's
process stopped (the client can detach from a container that keeps
running), so the script first waits up to `RESTORE_WAIT_SECONDS` for
the container to be gone, then starts only the indexer and leaves
`mcp-server` stopped. Check the indexer log, then run `make up`. If the
container is still running after the wait, nothing is started: wait
for it (`docker wait restore-index-<pid>`, the name is in the
message), then run `make up` and check the indexer log.

The backup file and every directory above it must be yours (or
root's) and writable only by you, with no ACL entry that gives another
account access (on macOS, deny-only entries such as the one on home
directories are fine), unless the directory is sticky like `/tmp`, and
the file must not be a symbolic link: another account
could otherwise swap in a crafted index, which the integrity and schema
checks cannot tell apart. The directories are checked both as written
in `BACKUP` and after resolving symbolic links, and a symbolic link
among the directory components must be yours (or root's, like `/tmp`
on macOS): its owner could otherwise repoint it between the checks and
the open. Directories made by `make backup-index` already qualify.

## Embedder identity mismatch

The indexer or mcp-server exits at startup with "The configured
embedder is not the one that built this index (...)". The index records
the embedder that built it (see docs/architecture.md, "Embedder
identity record"), and the one configured now differs in the fields
the message lists:

- `EMBED_MODEL`, `endpoint` or `dimensions`: `EMBED_MODEL` or
  `EMBED_BASE_URL` changed (the endpoint is the resolved URL, so
  `EMBED_BASE_URL=default` reads as `https://api.openai.com/v1`, whatever
  `OPENAI_BASE_URL` says). Both services must use the same values.
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

"embedder at ... rejected the warmup request (APIStatusError:
status=402)" from the indexer means the embedder refused its first
request; "Embedder calibration request failed" is the same for the
calibration request right after it. Startup errors carry the error
type and status code, never the provider's response text, so read the
status: 401 or 403 is
the API key, 402 is the provider account (no balance or billing), 404
is usually `EMBED_MODEL`. The indexer exits and Docker restarts it
until the account or setting is fixed. "did not become ready within
...s (last error: ...)" means every attempt within the connect
deadline failed with a connection error, a timeout, a 5xx, 408 or 429.

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
brake). The reaper never deletes Maildir files: a reaped message's
`.eml` stays on disk (#728 tracks deleting them on the mbsync side).

The indexer reads these settings once at startup, so a change takes
effect only when the `indexer` container is recreated. After editing
`.env`, run `make up`: Compose recreates every container whose
configuration changed. `docker compose restart` is not enough, because
a restarted container keeps the environment it was created with. If
the stack was started with an overlay (such as
`docker-compose.hardened.yml`), run `docker compose up -d` with the
same `-f` files instead, or the recreated container drops the overlay.
To confirm the new value reached the container:

```bash
docker compose exec indexer env | grep '^INDEXER_DELETION_'
```

Defaults — 7-day grace window, 5% mass-delete brake — are
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
retrying, deferred, parked trashed and dead counts and whether the
index is current (parked trashed and dead jobs do not count against
it; see [`get_mailbox_status`](mcp-tools.md#get_mailbox_status)). For the
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

## Indexer health in the log

The indexer's recurring work logs counts, durations, fixed text and
exception type names only, never folder names, Message-IDs, addresses
or mail text (`docker compose logs indexer`).

Embedder retries and outages:

- `embed retry attempt=<n>/3 after <error>`: an embed request failed
  with a transient error (a 429 or 408, a 5xx, a timeout or a
  connection error) and is being retried after a 2 to 10 s backoff.
  INFO, or WARNING before the last attempt. `<error>` is the exception
  type, plus the HTTP status for a provider error. Occasional lines are
  normal; a steady stream means the provider is rate limiting or
  struggling and indexing is slowing down. These lines share the
  20-per-5-minutes budget of the attachment WARNINGs (see "Attachment
  text or message content missing from search"); the rest are counted
  as `suppressed_lines` on the queue heartbeat (below).
- `embed request recovered on attempt <n>/3` (INFO): the retried
  request went through. It is logged whenever a retry line of that
  request was, budget or not, so a retry line with no recovery line
  after it is a request that failed all three attempts (see the ERROR
  lines below). When every retry line of a request was withheld, the
  recovery is withheld with them.
- `embedder unavailable (...)` or `embedder rejected credentials or
  model (...)` (ERROR): a batch failed after its retries and a probe
  confirmed the embedder itself is down; indexing pauses (see "Tuning
  indexing retries").
- `embedder recovered after <n> failure(s), paused <s>s; indexing
  resumed` (INFO): the first successful embed request after an outage.
  `<n>` is the number of times the pause was extended, `<s>` how long
  indexing was paused in all. A batch with nothing new to embed sends
  no request and does not count.

Each recurring step below logs a failure every time it fails, and one
`<step> recovered after <n> failure(s) over <s>s` line (INFO) on its
first success after failing, where `<s>` is the time since its first
failure. A failure line with no recovery line after it means the step
is still failing.

| Step | Failure line | When it runs |
|---|---|---|
| `health file refresh` | `health file refresh failed: <type>` (WARNING) | Per message, embed request and attachment page |
| `ingestion state recording` | `recording ingestion state failed: <type>` (ERROR) | At most every 30 s, retried on each heartbeat until it succeeds |
| `Maildir watch refresh` | `Maildir watch refresh failed: <type>` (ERROR) | After each mbsync sync, and every `INDEXER_RECOVERY_SWEEP_INTERVAL_SECS` |
| `periodic Maildir rescan` | `periodic Maildir rescan failed: <type>` (ERROR) | Every `INDEXER_RECOVERY_SWEEP_INTERVAL_SECS` (30 min), and after an inotify queue overflow (below) |
| `periodic rename sweep` | `periodic rename sweep failed: <type>` (WARNING) | Before each periodic Maildir rescan; the rescan's walk runs either way |
| `periodic reconciliation` | `periodic reconciliation failed: <type>` (ERROR) | Every `INDEXER_DELETION_SWEEP_INTERVAL_SECS`, with deletion reconciliation on |
| `reaped-record prune` | `reaped-record prune failed: <type>` (ERROR) | At startup and with each reconciliation interval |
| `wal checkpoint` | `wal checkpoint failed: <type>` (ERROR) | At startup and every `INDEXER_WAL_CHECKPOINT_INTERVAL_SECS` (10 min) |

The health-file and ingestion-state failures can repeat many times a
second, so they share the same 20-per-5-minutes budget as the embed
retries; their recovery line still counts every failure.

Maildir watcher and walks (#870):

- `Maildir watcher thread stopped; new mail is found only by the
  periodic rescan; exiting so the container restarts` (ERROR): the
  watchdog thread that turns mbsync's file events into indexing jobs
  died (an exception escaped one of its callbacks, for example a
  locked database or a full disk; Python prints the thread's traceback
  just before this line). The indexer exits with status 1 and Compose
  restarts it with a fresh watcher; the startup walk queues any mail
  delivered in between. The check runs on every heartbeat (per
  message, embed request and attachment page), during the initial
  index as well as the steady-state loop, and the heartbeat is not
  written once the thread is found dead. A restart loop with this
  line means the cause persists: read the traceback above it.
- `Maildir watcher: inotify event queue overflowed and dropped events;
  a Maildir walk will queue the mail they announced` (WARNING): a
  burst of file events (a large folder delivered in one sync) filled
  the kernel's inotify queue (its length is the Docker host kernel's
  `fs.inotify.max_queued_events`) and Linux dropped the events after
  it (#1108). The indexer
  runs a Maildir walk (`maintenance pass=rescan` follows) at once,
  retried every 60 s while it fails, and until a walk completes
  `get_mailbox_status` does not count a sync as ingested from the
  watcher alone. `Maildir watcher: recovered from <n> inotify queue
  overflow(s); a Maildir walk queued the mail their dropped events
  announced` (INFO) ends the episode. The WARNING shares the
  20-per-5-minutes budget (the rest count as `suppressed_lines`); `<n>`
  counts every overflow. An occasional pair is harmless: the walk
  queues the mail either way. A
  `Maildir watcher: inotify overflow detection unavailable` WARNING at
  startup means the installed watchdog no longer has the parser the
  indexer wraps; overflows then go unreported and the periodic rescan
  queues the mail.
- `Maildir walk: skipped <n> director(ies) it could not read; their
  mail is not indexed until they are readable` (WARNING), after a
  startup or periodic Maildir walk, and `Maildir watch: <n>
  director(ies) could not be read and are not watched until they are
  readable` (WARNING), after a watch schedule or refresh. A folder the
  indexer's UID cannot enter is neither walked nor watched, so its
  mail is missing from the index until a later walk or refresh finds
  it readable. mbsync creates each new folder 0700 and opens it after
  its sync, so a line during a sync is transient; one that repeats
  after the sync means the permission repair did not reach the folder
  (see "Index is empty after startup"). The count is of directories,
  never their names.

Queue and maintenance (all INFO unless noted):

- `queue: pending=<n> retrying=<n> deferred_permission=<n>
  parked_trashed=<n> dead=<n> oldest_due_age=<s>s; deferrals since last
  heartbeat: parse=<n> embed=<n> trashed=<n>; suppressed_lines=<n>`,
  every 5 minutes, during the initial index too. `pending` jobs have
  never failed; `retrying` jobs failed or were deferred by an embedder
  outage; `deferred_permission` jobs could not be read yet (mbsync
  opens new files to the indexer only after its sync; deferred for up
  to 24 h, after which a still-unreadable file takes the normal retry
  path and counts as `retrying` until it is dead);
  `parked_trashed` jobs belong to trashed messages waiting for the
  reaper, which is normal in mirror mode, not a failure. A growing
  `oldest_due_age` means due jobs are not being drained (an embedder
  outage pauses draining; see above), except during a reparse, whose
  jobs wait behind newer mail while `reparse: remaining=` falls (see
  [Reparse or rebuild after an upgrade](#reparse-or-rebuild-after-an-upgrade)).
  The deferral counts are `defer`
  calls since the previous heartbeat, by stage. `suppressed_lines` is
  how many embed retry and recovery, health-file and ingestion-state
  lines the shared rate limit withheld since the previous heartbeat
  (counted apart from the attachment WARNINGs, so they never make the
  attachments line a WARNING). `queue heartbeat failed: <type>`
  (WARNING) if the counts could not be read.
- `re-queued <n> message(s) whose attachments were extracted by an
  older extractor version (...); skipped <n> dead-lettered (run make
  requeue-dead to refresh them).`, at startup after an extractor
  change. WARNING when any dead-lettered message was skipped: those
  keep their old attachment text until you run `make requeue-dead`.
- `maintenance pass=rescan ms=<ms> seen=<n> queued=<n>
  skipped_dead=<n>`, after each periodic Maildir rescan, even when it
  queued nothing. `seen` is message files walked, `queued` the ones
  with no event that the rescan picked up.
- `maintenance pass=reconcile ms=<ms> tombstoned=<n> cleared=<n>
  renamed=<n> missing=<n> threads_reaped=<n> threads_rebuilt=<n>
  blocked_threads=<n> brake=<ok|tripped|forced>`, after each deletion
  reconciliation pass (mirror mode only). `brake=tripped` means the
  mass-delete brake held this pass's reaps back (see "Deletion
  reconciliation"); `forced` means `INDEXER_DELETION_FORCE=true`
  disables the brake.
- `maintenance pass=watch_refresh ms=<ms> watches=<n>`, after each
  periodic Maildir watch refresh; `watches` is the number of
  directories readable when the watch was last scheduled.

WAL and storage, after each WAL maintenance pass (at startup and every
`INDEXER_WAL_CHECKPOINT_INTERVAL_SECS`, 10 minutes by default):

- `storage: db=<MB>MB wal=<MB>MB free_disk=<MB>MB` (INFO): the size of
  `mail.db`, of its `-wal` file, and the free space on the volume
  holding them, in MiB (rounded down). `storage: size check failed
  (<type>)` (WARNING) if they could not be read.
- `wal checkpoint blocked <n> times in a row; WAL=<pages> pages`
  (WARNING), from the third blocked checkpoint in a row (30 minutes at
  the default interval) and on every blocked pass after it. Something
  is holding a read transaction open on the database (a long query in
  mcp-server, or an `sqlite3` shell left open), so the WAL cannot be
  truncated and keeps growing; watch `wal=` in the storage line. A
  single blocked pass is normal and logged at DEBUG only.
- `wal checkpoint unblocked after <n> blocked pass(es)` (INFO): the
  first checkpoint that completed after a warned run.

## Reading a tool call's log line

Every MCP tool logs one completion line per call on the `mcp.timings`
logger, whether it succeeded or failed (#886):

```text
tool=query_messages outcome=ok total_ms=12.4 stages_ms={} counts={'total_matches': 214, 'returned': 25} config={}
tool=get_thread outcome=error total_ms=1.9 stages_ms={} counts={} config={}
```

- `outcome` is `ok` or `error`. For the retrieval tools and
  `get_mailbox_status`, an `error` line is preceded by a WARNING or
  ERROR line from the tool's own logger (`mcp.tools.retrieval`,
  `mcp.tools.system`) naming the cause as fixed text or an exception
  type, for example
  `get_thread failed: not found`, `rejected invalid argument:
  list_threads.filter_type` or `query_messages error: OperationalError`.
  A rejected argument (every tool, keyed by tool and field, #1039) is
  logged once per key per minute; later repeats in that minute are
  counted into one `rejected invalid arguments in the last <N>s:
  <tool>.<field>=<count> ...` line, logged when the next rejection
  arrives after the minute ends. An argument the tool's own argument
  model refuses (a wrong type, for example `limit="abc"`) never
  reaches the handler or this log: FastMCP logs its own `Invalid
  arguments for tool '<tool>': {'error_count': ..., 'error_types':
  [...]}` WARNING per call (codes and counts only, never the value),
  with no rate limit (#1131).
- `total_ms` is the whole call; `stages_ms` the timed stages
  (retrieval lanes, embedding, rerank, inference) that ran.
- `counts` holds result counts: `returned`, `total_matches` and
  `indeterminate` (messages the filters could neither accept nor
  reject; non-zero means the count is not complete) for
  `query_messages`, `messages`, `threads`, `contacts`, `folders` for the
  other retrieval tools, and the lane and degradation counts of the
  search and intelligence tools.
- `config` names the rerank and inference modes the call used.

The line never carries arguments, Message-IDs, addresses, names or mail
text. The full list of stages and counts is in
[Stage timings](mcp-tools.md#stage-timings-in-the-server-log). A call
with no completion line did not reach the tool, for example a request
rejected with 401 (see
[MCP client gets 401 Unauthorized](#mcp-client-gets-401-unauthorized)).

## A tool call reports degraded retrieval

A `degraded_<lane>` count on a tool's `mcp.timings` line (see
[Stage timings](mcp-tools.md#stage-timings-in-the-server-log)) means a
retrieval lane failed and the call carried on without it, so the
results are worse than usual although `outcome=ok`. A standalone
`mcp.sqlite` or `mcp.reranker` WARNING names the exception type at the
same moment.

- `degraded_rerank` on most calls: the rerank provider is failing (the
  `mcp.reranker` warning gives the status code). Check
  `RERANK_BASE_URL`, `RERANK_MODEL`, the key in
  `.secrets/rerank_api_key.txt` and `RERANK_TIMEOUT_SECS`, or set
  `RERANK_MODE=none` until the provider is back.
- `degraded_thread_vec` / `degraded_chunk_vec`, or any `_fts` /
  `attachment_` lane on every call: the index is missing a table or is
  corrupt. Check the indexer's log, then rebuild as in
  [Indexer refuses to start](#indexer-refuses-to-start--wipe-the-sqlite-volume).

## The log shows "token limit hit"

`ask_mailbox`, `summarize_thread`, `extract_from_emails` and the
experimental `brief_issue` and `check_conclusion` log one WARNING per
call that ran into a token limit (#865, #951), for example:

```text
token limit hit: tool=ask_mailbox limits=evidence_budget outputs_cut=0 threads_dropped=1 passages_omitted=6 passages_truncated=1 prompt_tokens=2950 prompt_budget_tokens=3008 max_tokens=1024
```

The line carries counts and settings only. Each limit also reaches
the caller: a cut reply carries a truncation notice (for `brief_issue`
and `check_conclusion`, `status: "truncated"` with
`truncation_reason`), the evidence that `ask_mailbox`,
`summarize_thread` and `extract_from_emails` leave out for the window
is disclosed in their coverage or evidence note (#949), and
`prompt_over_budget` is an error. The exception is evidence that
`brief_issue` and `check_conclusion` leave out for the window: only
the model is told, in the prompt, so this log line is where it shows.
`limits` names which limits the call hit:

- `output_max_tokens`: the model stopped at `INFERENCE_MAX_TOKENS`, so
  the answer or summary was cut off (`outputs_cut` counts every cut
  reply, whatever stopped it; for `extract_from_emails`, the threads
  whose reply was lost). A reply cut before any text fails the call
  with an error, and the line is still logged. Raise
  `INFERENCE_MAX_TOKENS`. The reply reserve comes out of
  `INFERENCE_CONTEXT_TOKENS`, so raise that by the same amount if the
  model's window allows, or the prompt allowance shrinks.
- `context_window`: the model's own context window filled before the
  reply reached `INFERENCE_MAX_TOKENS` (Anthropic's
  `model_context_window_exceeded` stop; `context_window_cuts` counts
  these replies). Raising `INFERENCE_MAX_TOKENS` does not help.
  Either `INFERENCE_CONTEXT_TOKENS` is set larger than the model's
  real window, or the prompt tokenizes more densely than the
  three-characters-per-token estimate (CJK scripts, long digit or
  base64 runs). Lower it to the model's real window, or below it for
  dense mail, or choose a model with a larger one. The caller's truncation notice (or, for a reply cut before any
  text, the error) says the same: it names `INFERENCE_CONTEXT_TOKENS`,
  not `INFERENCE_MAX_TOKENS` (#890). So does `extract_from_emails`'s
  `Incomplete:` line, which counts the threads cut at the context
  window apart from those cut at `INFERENCE_MAX_TOKENS` (#950), and
  `brief_issue` and `check_conclusion` return
  `truncation_reason: "context_window"` (#951).
- `evidence_budget`: the model window, not the fixed per-thread cap,
  left out passages (`passages_omitted`), cut them short
  (`passages_truncated`) or dropped lower-ranked threads
  (`threads_dropped`; `threads_cut` for `extract_from_emails`). The
  passage counts are the window's only: when it dropped a thread but
  the per-thread cap trimmed the rest, they are 0 and the trim is
  counted as `evidence_capped_threads` instead. For `summarize_thread`
  the counts are characters of mail context: `context_chars_kept` out
  of the `context_chars_wanted` the default window would show. The
  answer may miss facts. Raise `INFERENCE_CONTEXT_TOKENS` up to the
  model's real window.
- `prompt_over_budget`: the request failed before any inference
  because the instructions, request and headers alone (`prompt_tokens`,
  estimated) are larger than the prompt allowance
  (`prompt_budget_tokens`, what `INFERENCE_CONTEXT_TOKENS` leaves after
  `INFERENCE_MAX_TOKENS`). Shorten the request or schema, or raise
  `INFERENCE_CONTEXT_TOKENS`.

`prompt_tokens` is the estimated size of the prompt sent (the largest
one for `extract_from_emails`; for it, `brief_issue` and
`check_conclusion`, including the reply schema structured outputs
add), counted at three characters per token. For `ask_mailbox`,
`summarize_thread`, `brief_issue` and `check_conclusion` it is the
last prompt sent: the repair prompt whenever a repair call was made,
whether or not its reply was cut (#984).

The call's own `mcp.timings` line also carries a
`token_limit_<limit>` count for each limit it hit, so the warning can
be matched to its call when several run at once.

Trimming to the fixed per-thread evidence budget (2,000 characters per
thread) is not a token limit: no setting changes it, so it logs no
warning. It is counted as `evidence_capped_threads` on the call's
`mcp.timings` line instead.

## Attachment text or message content missing from search

An attachment whose text could not be extracted is still indexed by
filename and type, but its contents are not searchable. The indexer
logs these outcomes with counts, extractor names and exception types
only, never filenames or text (`make logs`):

- `extractor <module> failed (dispatch_via=<mime|extension|...>):
  <ExceptionType>` (WARNING), per extraction that fails: an encrypted
  PDF that needs a password, a Tesseract error or timeout, a DOCX,
  XLSX or PPTX the parser rejects (`PptxRelationshipChainError` is a
  deck whose parts are chained too deep to open, and
  `DocxRelationshipChainError` the same for a `.docx` or `.dotx`), or
  (`zip uncompressed-size cap exceeded`) a DOCX, XLSX or
  PPTX that would decompress past its cap. For a legacy `.doc`,
  `.xls` or `.ppt` (#935, #957) the type names the tool's fate:
  `ToolTimeoutError`, `ToolCrashError` (killed by a signal, including
  each tool's CPU limit), `ToolExitError` (an error, including each
  tool's memory limit, and any deck the
  `.ppt` reader rejects or that needs more than its 128 MiB heap; a
  password-protected deck is `unsupported` instead, below),
  `ToolNotFoundError` (catdoc or the
  `.ppt` Java runtime missing from the image) or `XlsOutputError`.
  Many of these at
  once usually means the OCR toolchain or a parser library is
  broken, not the mail. A failed result is cached for 7 days, then
  retried when the same bytes arrive again.
- `extractor <module> declined (dispatch_via=<mime|extension|...>):
  <fixed text>; recorded unsupported, not retried` (WARNING): the
  extractor refused the file in a way the same bytes always repeat, so
  the result is cached `unsupported` for good instead of `failed`
  (#931): a PDF that needs an open password or exceeds pypdf's limits,
  a workbook over the XLSX eager-part budget, or a deck or document
  over the PPTX / DOCX pre-open package budgets (#1032: a deck whose
  XML would decompress past 32 MiB, or with more than 20,000 members
  or 8 MiB of relationship parts; a `.docx` or `.dotx` past 32 MiB,
  5,000 members or 4 MiB of relationship parts; either one whose
  members, media included, declare more than 48 MiB in all, #1033), or a password-protected
  legacy `.ppt` (#983). The file stays
  searchable by filename and type only. A `.docx` or `.dotx` over a
  budget that was recorded `failed` (`DocxPackageBudgetError`) before
  #1032 is not re-queued at startup, because the `docx` extractor
  version was deliberately not bumped while its walk after the open is
  unbudgeted (#1031). It stays `failed` until the same bytes are
  processed again (a new occurrence, or the message reprocessed for
  another reason) more than 7 days after it was recorded; that re-run
  reads the central directory once, never opens the document, and
  records it `unsupported`. A `docx` bump after #1031 converts the
  rest at the next start. A password-protected `.ppt` recorded `failed`
  (`ToolExitError`) before #983 converts the same way, since the `ppt`
  version was not bumped either: on its first re-run more than 7 days
  after it was recorded.
- `PDF OCR fallback failed: <ExceptionType>` (WARNING): OCR of a PDF's
  pages without a text layer raised (a Tesseract error or timeout). A
  PDF with enough digital text keeps it and loses the scanned pages;
  otherwise the extraction fails as above.
- `pdf OCR capped at <N> of <M> scanned pages` (WARNING): a scanned
  PDF had more pages without a text layer than `INDEXER_OCR_MAX_PAGES`;
  the pages past the cap are not read. Every capped PDF is also counted
  in the attachments line below (`ocr_capped_pdfs`, `ocr_pages_skipped`).
  Raising the cap applies only to PDFs extracted afterwards, since the
  result is cached. Known limitation (#891): the cap is logged and
  counted on the first extraction only. A later message carrying the
  same PDF is served from the extraction cache and reports a plain
  `success`, with no cap line and no `ocr_capped_pdfs` count, although
  the cached text still lacks the unread pages.
- `image OCR capped at <N> of at least <N+1> frames` (WARNING): a
  multipage TIFF had more frames than `INDEXER_OCR_MAX_PAGES`; the
  frames past the cap are not read. The indexer looks one frame past
  the cap rather than count every frame, so the total is reported as
  "at least". `image OCR capped at <N> frames; the next frame could not
  be read (<ExceptionType>)` is the same cap when that frame directory
  is corrupt; the frames already read are still indexed. Each is
  counted as `ocr_capped_images` in the attachments line below. The
  same caching and #891 limitation as the PDF cap line apply.
- `extractor cap <name>: <fixed text and counts>` (WARNING): a cap
  inside an extractor cut the text it returned (#903). Logged once per
  cap per extraction, and counted as `extractor_caps` in the
  attachments line below. The caps, by name:
  - `extracted_chars`: the extracted text was longer than
    `INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS`; the rest is not stored.
  - `pdf_digital_pages`: the PDF has more pages than
    `INDEXER_PDF_MAX_DIGITAL_PAGES`; the pages past it are not read.
  - `pdf_ocr_dpi`: a scanned page is too large to render at 200 dpi
    within the 10-megapixel page budget, so the PDF's OCR ran at the
    lower DPI the line names, which reads small print less reliably.
  - `xlsx_sheet_nodes`, `xlsx_row_nodes`, `xlsx_tag_bytes`: a
    worksheet's XML crossed a node budget (5,000,000 across the
    workbook, 131,072 in one row) or a 1 MB start tag; that worksheet is
    cut before the row that crossed it. Later worksheets are still read
    within the budget left: after a row or tag cut that is the
    workbook's remaining budget, and after a workbook-wide cut only what
    was left before the cut row, so a later worksheet larger than that
    is cut or read as empty as well.
  - `xlsx_expanded_cells`, `xlsx_text_chars`: the walk over a
    workbook's cells stopped at its cell budget (5,000,000, counting a
    row as 64 cells) or its 10,000,000-character text budget.
  - `xls_sheets`, `xls_expanded_cells`, `xls_text_chars`: the same walk
    over a legacy `.xls` stopped at 1,024 sheets or at the cell or text
    budget above (#935).
  - `doc_output_bytes`: catdoc wrote more than 8 MiB for a legacy
    `.doc`; the rest is not read (#935).
  - `ppt_output_bytes`: the `.ppt` reader wrote more than 8 MiB for a
    legacy `.ppt`; the rest is not read (#957).
  - `pptx_slides`, `pptx_shapes`, `pptx_table_cells`,
    `pptx_text_chars`: the walk over a PowerPoint deck stopped at its
    slide budget (5,000 slide-list entries), shape budget (100,000,
    counting groups and the shapes in them), table budget (200,000 rows
    and cells) or 10,000,000-character text budget; the slides after it
    are not read.

  The other caps either skip or fail the whole attachment and show as
  `too_large` or `failed` instead (`INDEXER_ATTACHMENT_MAX_BYTES`, the
  zip, image-pixel and XLSX whole-part caps, the OCR timeout, the
  legacy-Office tool timeouts and the `.xls` child's and the `.ppt`
  reader's memory and CPU limits); the OCR
  page caps have their own lines above. Like the OCR cap, a cap is reported
  on the first extraction only: the cached text is served afterwards.
- These per-item WARNINGs (failed extractions, OCR fallback failures,
  OCR and extractor caps, and the parser-cap line described below,
  together) are capped at 20
  per 5 minutes, so a stream of crafted mail cannot flood the log. The rest
  are counted as `warnings_suppressed` in the attachments line below.
  The budget is shared with the embed retry, health-file and
  ingestion-state lines (see "Indexer health in the log"), but those
  are counted as `suppressed_lines` on the queue heartbeat, never
  here.
- `attachments n=<total> success= failed= unsupported= too_large=
  ocr_disabled= empty= cached= pdf_pages_failed=
  pdf_pages_unrecovered= ocr_capped_pdfs= ocr_pages_skipped=
  ocr_capped_images= extractor_caps= parser_caps_messages=
  parser_recipients_merged_messages= parser_sender_ambiguous_messages=
  warnings_suppressed=`: the attachments of the messages committed since the previous line, by outcome. It is a
  WARNING when any of `failed`, `unsupported`, `too_large`,
  `ocr_disabled`, `pdf_pages_unrecovered`, `ocr_capped_pdfs`,
  `ocr_pages_skipped`, `ocr_capped_images`, `extractor_caps`,
  `parser_caps_messages` or `warnings_suppressed` is above zero (some attachment text is not
  searchable), and INFO otherwise. `pdf_pages_failed` alone does not
  make it a WARNING (see below).
  - When it is logged: during the initial index, with the timing summary
    once at least 25 messages have been drained since the last one (each
    batch, at the default `INITIAL_INDEX_BATCH_SIZE=50`), and once at
    the end. Afterwards, with the steady-state timing summary: every 25
    messages, when a drain leaves no more ready jobs (the end of a
    burst), and at least every 5 minutes while counts are pending. At
    most one line is logged per 60 seconds (the initial index's final
    line excepted); counts held back are carried into the next line,
    so none are lost, and the 5-minute flush still applies.
  - What the outcomes mean: `cached` counts attachments served from the
    extraction cache instead of extracted again. `unsupported` is a type
    no extractor reads, and password-protected Office files and other
    OLE2 files not labelled `.doc` / `.xls` / `.ppt`, recorded with
    "OLE2 compound file" rather than as `failed`, so they are not
    retried (#694); a `.ppt`-labelled file that is not OLE2 is recorded
    with "not an OLE2 compound file (labelled legacy .ppt)" (#957).
    Genuine legacy `.doc`, `.xls` and `.ppt` files are extracted with
    catdoc, xlrd and Apache POI (#935, #957); each `.ppt` starts a Java
    process, about 0.2 to 0.4 s of CPU; a crashed,
    timed-out or over-limit run is `failed` with a fixed error type such
    as `ToolTimeoutError` or `ToolExitError` (see `docs/architecture.md`,
    "Extractor dispatch"). Binary files (PDF, ZIP, OLE2, PNG, JPEG,
    GIF) sent as text are recorded with "binary payload labelled as
    text" (#932). It also counts PDFs that need an open password or
    exceed pypdf's limits, workbooks over the XLSX eager-part budget,
    decks and documents over the PPTX / DOCX pre-open package budgets,
    and password-protected legacy `.ppt` decks, which fail the same way
    every time (#931, #1032, #983). `too_large` is over
    `INDEXER_ATTACHMENT_MAX_BYTES`, and `ocr_disabled` is an image or
    scanned PDF skipped while `INDEXER_OCR_ENABLED=false` (re-extracted
    once OCR is turned on).
  - What the extraction counts mean: `pdf_pages_failed` is a diagnostic
    count of PDF pages whose text layer pypdf could not read. With OCR
    on, such a page is OCR'd (within `INDEXER_OCR_MAX_PAGES`) and its
    text may be recovered, so the count alone does not mean text is
    missing. A steady rise across ordinary PDFs points at a pypdf
    regression. `pdf_pages_unrecovered` counts those pages whose text
    was never recovered: OCR is off, the PDF had enough digital text on
    its other pages to return before OCR, OCR read no text from the
    page, or the OCR cap left it unread. These pages are missing from
    search.
    `ocr_capped_pdfs` counts scanned PDFs whose OCR stopped at
    `INDEXER_OCR_MAX_PAGES`, and `ocr_pages_skipped` the scanned pages
    they left unread. `ocr_capped_images` counts multipage TIFFs whose
    OCR stopped at the cap with a frame left unread; their unread
    frames are not counted.
    `extractor_caps` counts the extractor caps above, one per cap per
    extraction.
  - How retries count: the outcomes are counted once per committed
    message, so a message retried after an embedder outage counts once.
    The extraction counts (`pdf_pages_failed`,
    `pdf_pages_unrecovered`, `ocr_capped_pdfs`, `ocr_pages_skipped`,
    `ocr_capped_images`, `extractor_caps`, `warnings_suppressed`) and the
    per-attachment WARNINGs count every extraction attempt, retries included, and
    `parser_caps_messages` every parse of a capped message (see
    below).

The parser also caps the work one message can cost. A cap that loses
content logs one WARNING line for that message, with its Maildir path
and the caps that fired, by name and count:
`parser work caps dropped content from <path>: body_parts=5,address_header=1`.
The message is still indexed, without that content. These lines share
the 20-per-5-minutes limit above. Every capped message, logged or not,
is counted as `parser_caps_messages` in the attachments line, which
it makes a WARNING.

The header caps count the same way (#902): `subject_length` when a
decoded subject over 2,000 characters was cut to that length,
`in_reply_to_length` when an `In-Reply-To` over 998 characters was
dropped, and `references_length` for each `References` entry over 998
characters dropped (the rest are kept, so threading uses them). A
value exactly at its cap is kept whole and not counted.

An attached email (or another container part) counts only when
something searchable is lost:

- The attachments inside a base64 or quoted-printable attached email
  are not read.
- The container's own payload is emptied while an extractor would have
  read it, for example a delivery report named `status.txt`.

An attached email sent without a transfer encoding is still walked, so
the attachments inside it are kept, and emptying a payload that no
extractor reads (`.eml`) is not logged.

| Cap | What was dropped |
|---|---|
| `attached_depth` | An attached email nested more than 20 levels deep (or 20 transfer-encoded levels) |
| `attached_fields` | The same, once the message's attached emails exceed the per-message part and header budget |
| `transport_decode` | A base64 or quoted-printable attached email that does not decode: the attachments inside it are not read |
| `decoded_bytes` | The same, past 64 MB of decoded attached emails per message |
| `container_serialize` | A container the serializer refuses (a malformed header), when its payload would be extracted |
| `body_parts` | Text parts past the 200th, left out of the body: only those that could have been part of it, so an alternative rendering after the one the body uses is not counted |
| `mime_parts` | Every MIME part past the 10,000th (the message itself and the parts inside attachments count): their text and attachments are not read. Counted once per message |
| `address_header` | Every recipient of a `From`, `To` or `Cc` header over 256,000 characters (checked before the header is decoded) |
| `address_element` | One address-list entry over 128,000 characters |
| `address_length` | One address over 998 characters |
| `address_fields` | Any `From`, `To` or `Cc` after the message's first 10,000 header fields; the sender is then ambiguous (below). Counted once per message |
| `address_occurrences` | Every recipient of a `From`, `To` or `Cc` header past the message's 64th such header |
| `address_chars` | Every recipient of one past 768,000 characters of `From`, `To` and `Cc` in all |
| `address_elements` | Address-list entries past the message's 20,000th, unparsed |
| `address_count` | Addresses past the message's 10,000th kept (one participant row each), unparsed |

The caps bound what crafted mail can cost the single indexing worker,
so they are not configurable. Ordinary mail does not reach them.

### Repeated `From`, `To` or `Cc` headers

A message may repeat an address header (#1144). Nothing is lost for a
repeated `To` or `Cc`: every occurrence is read, in order, into that
role, and the message logs one INFO line, `parser merged <n> repeated
To/Cc headers in <path>`, counted as `parser_recipients_merged_messages`
in the attachments line. A repeated `From` is not merged: the first
header is kept as the sender, the others are counted but not read, and
the message is marked `sender_ambiguous` (as is one whose header scan
stopped at `address_fields`). It logs one WARNING, `parser kept the first
of <n> From headers in <path>; sender ambiguous, no source authority or
subject-fallback threading`, and counts as
`parser_sender_ambiguous_messages`. Such a message never matches an
`authority_class` filter and is never joined to a thread by subject
alone (`In-Reply-To` and `References` still apply);
`docs/mcp-tools.md`, "Sender attribution", says what clients see. These
lines share the 20-per-5-minutes limit above, and name only the path
and counts, never an address; one the limit withholds is counted as
`suppressed_lines` on the queue heartbeat, not as `warnings_suppressed`,
since it loses no attachment text.

Only messages assessed safe supply the correspondent evidence a
subject-only merge needs. Until the reparse after the v2 upgrade
reaches a thread's messages, a new message is not merged into that
thread by subject alone and keeps its own thread; the line `subject
fallback rejected <n> candidate thread(s) without assessed
correspondents for <path>` (INFO, rate limited) counts these. A
reparse keeps each message's thread, so the split is permanent for a
thread started in that window, and for a thread whose messages are
dead-lettered (they stay unassessed). An ambiguous message linked by
`In-Reply-To` or `References` still joins its thread and can still
move the thread's last-activity date, and so the order in which
same-subject candidate threads are tried.

### Raw 8-bit headers

A header sent as raw bytes rather than RFC 2047 encoded-words (a
Subject, or a display name in `From`, `To` or `Cc`) is decoded as UTF-8
and logs nothing when the bytes are valid UTF-8 (#1147). Bytes in any
other encoding are decoded with replacement characters, and the
message logs one WARNING per affected header chunk, without the text:
`raw 8-bit header is not UTF-8; decoded 1 header chunk with replacement
characters`. A header without a parseable address is decoded twice (for
the address and for the From fallback), so it logs two. An RFC 2047
encoded-word in the same Subject or From fallback as raw bytes is kept
as sent, `=?...?=` text included (#1186), and logs `raw 8-bit header
holds encoded-words that were not decoded; kept 1 header as sent`
(WARNING); in a display name it is decoded. These lines
share the 20-per-5-minutes limit above and are counted as
`suppressed_lines` when withheld. A charset label the codec rejects
still logs `header encoded-word charset could not be decoded
(<ExceptionType>)` instead ([Attachment filename shows `=?utf-8?...?=`
text](#attachment-filename-shows-utf-8-text)).

### `authority_class` filters return nothing after an upgrade

Schema v2 (#1144) records whether each message's sender attribution is
safe, and only messages recorded as safe match an `authority_class`
filter. Mail indexed before the upgrade is not assessed until the
reparse the migration queues reaches it, so straight after the upgrade
the filters match nothing, and they fill in as the reparse drains. Its
progress is the `reparse: remaining=...` line and `make status`'s
`queue.reparse` ([Reparse or rebuild after an upgrade](#reparse-or-rebuild-after-an-upgrade)).
A message whose indexing job is dead-lettered stays unassessed until
`make requeue-dead`. Nothing else needs doing.

## Attachment filename shows `=?utf-8?...?=` text

Some clients send a long non-ASCII attachment name as RFC 2047
encoded-words (`=?utf-8?B?...?= =?utf-8?B?...?=`), which the standard
library does not decode in a filename parameter. The indexer decodes
them the same way as Subject (#924), so `search_attachments`,
`get_message` and filename search show the sender's name. A charset
label the codec rejects (unknown, `idna`, a NUL in the label) is
decoded as UTF-8 with replacement characters, as in Subject (#942),
and logged without the text: `header encoded-word charset could not be
decoded (<ExceptionType>); decoded 1 word as UTF-8` (WARNING, rate
limited, suppressed lines counted in the heartbeat). If
the encoded-words still do not decode to valid text, the indexer
keeps the filename as sent and logs, without the filename:
`attachment filename encoded-words could not be decoded
(<ExceptionType>); kept 1 filename as sent` (WARNING, under the same
20-per-5-minutes limit as the lines above).

The fix applies when a message is parsed. Filenames stored by an
earlier image keep the encoded text until their message is re-indexed;
a flag change or folder move does not re-parse a message, so to correct
them all rebuild the index as in
[Indexer refuses to start](#indexer-refuses-to-start--wipe-the-sqlite-volume).
The rebuild re-embeds every message, so it is optional. Extraction
dispatches by content type first, so for most such attachments only the
displayed name and filename search change; one sent as
`application/octet-stream` is also dispatched by its decoded extension
once re-indexed.

## ChatGPT says a tool call was blocked by OpenAI

ChatGPT reports "This tool call was blocked by OpenAI because we
couldn't determine the safety status of the request". This is a check
on OpenAI's side, made before ChatGPT sends the call, and nothing in
`.env`, the bearer token or the server changes the outcome. The check
is intermittent, and it also blocks tools that declare the read-only
safety hints (#919; background and sources in
[Safety annotations](mcp-tools.md#safety-annotations)).

1. Rule out a server-side failure. Look at the server log around the
   time of the refusal:

   ```bash
   docker compose logs mcp-server --since 10m | grep -E 'tool=<tool name>|rejected request'
   ```

   A tool that ran logs a `tool=<name> outcome=...` line on
   `mcp.timings` (see
   [Reading a tool call's log line](#reading-a-tool-calls-log-line)),
   and every tool except `list_folders` and `get_mailbox_status` also
   logs a `tool=<name> {...} withheld=[...]` line when it starts. A
   missing line shows only that the tool did not run, not by itself
   that the request never arrived: a request refused for its token,
   Host or Origin is logged as `rejected request: reason=<reason>`
   instead (see
   [MCP client gets 401 Unauthorized](#mcp-client-gets-401-unauthorized)),
   and a line with `outcome=error` is a server failure whose cause is
   on the WARNING or ERROR line before it. With neither, and ChatGPT
   showing this exact message, the block happened in ChatGPT.
2. Retry the identical call; it often succeeds on a later attempt.
   There is no server-side fix.
3. Approval settings are not a recommended fix. Setting the connector's
   approvals in ChatGPT to allow all actions without asking is reported
   to reduce the blocks, but it removes ChatGPT's per-call approval:
   every tool call then runs without a prompt, including calls that
   return mail to ChatGPT and the intelligence tools that send mail
   excerpts to the configured inference provider. Keep approvals on
   and retry instead.

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

The server logs rejected requests at WARNING as
`rejected request: reason=<reason>` (#878), and never logs the token,
the `Authorization` header or the Host and Origin values. So that a
prober cannot flood the log, only the first rejection per reason in
each 60-second window gets that line. The rest are counted, and with
the first rejection after the window ends the server logs
`rejected requests in the last <N>s: invalid_token=500 bad_origin=3`
(every rejection in that window, the first ones included; no line when
each reason was rejected only once). The reasons:

- `missing_token`: no `Authorization` header (`401`). The client is
  not configured to send the token at all.
- `invalid_token`: a header with a wrong token or another scheme
  (`401`). fastmcp's own per-request `Auth error returned` line is
  filtered out, so this rate-limited line is the record. Follow the
  steps above.
- `bad_host`: a Host other than `localhost`, `127.0.0.1`, `[::1]` or
  `mcp-server` (`421`). Point the client at `http://127.0.0.1:3000/mcp`.
- `bad_origin`: a browser `Origin` outside the same names over `http`
  (`403`), usually a web page trying to reach the server.

Repeated `bad_origin` or `invalid_token` lines, or large counts in the
per-window line, that your own clients do not explain mean something
on this machine is probing the server: a browser page, or a process
under another local account.

## mcp-server exits with "The MCP bearer token is missing or empty"

`.secrets/mcp_auth_token.txt` is empty (or holds only whitespace).
`make validate-env`, which `make up` runs first, catches this too.
Create a token with `make init-secrets` (when the file does not exist)
or `(umask 077; openssl rand -hex 32 > .secrets/mcp_auth_token.txt)`,
run `make up`, and
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
and change client URLs from `/sse` to `/mcp`.

## Bridge credentials expired / need to re-authenticate

Sign in again in the Bridge app. If its IMAP username or password
changed (the app shows them in the account's mailbox details), copy the
new username into `.env` and write the new password into
`.secrets/bridge_pass.txt`:

```bash
printf '%s' 'new-bridge-generated-pass' > .secrets/bridge_pass.txt
chmod 600 .secrets/bridge_pass.txt
make up
```

Your email index is in a separate volume (`sqlite-volume`) and is not affected.

If the app was reset or reinstalled, it may also present a new TLS
cert. mbsync then refuses to sync with `the Bridge certificate does not
match BRIDGE_CERT_FINGERPRINT` (and, once that is updated,
`Bridge cert fingerprint does not match pinned value`). That is the pin
working as intended. Take the new fingerprint
([setup](setup.md#4-set-up-the-proton-mail-bridge-app), step 4.3), set
it in `.env`, and accept the new cert with the two-step rotation in
[mbsync refuses to sync — Bridge cert pin mismatch](#mbsync-refuses-to-sync--bridge-cert-pin-mismatch):
recreate `mbsync` once with `BRIDGE_CERT_PIN_ROTATE=true`, check the
`rotating pin` warning, then recreate it with
`BRIDGE_CERT_PIN_ROTATE=false` to re-enable pin enforcement.

Once the pin is rotated, the reset Bridge may number the mailbox's
messages differently. If mbsync then reports `UIDVALIDITY genuinely
changed`, see
[mbsync reports a UIDVALIDITY change](#mbsync-reports-a-uidvalidity-change).

## mbsync refuses to sync — Bridge cert pin mismatch

On every start `mbsync` extracts Bridge's TLS cert, computes its SHA-256
fingerprint and refuses it unless it equals `BRIDGE_CERT_FINGERPRINT`.
On first boot it then saves the fingerprint to a persistent state volume
(`mbsync-state`).
On every subsequent boot the freshly extracted cert is compared to the
pinned fingerprint. A mismatch is treated as a security event and
`mbsync` refuses to sync. Log output looks like:

```
>>> ERROR: Bridge cert fingerprint does not match pinned value — refusing to sync.
>>>   pinned:  sha256:<old>
>>>   current: sha256:<new>
```

Legitimate cert rotations happen when the Bridge app is reset or
reinstalled, or an update replaces its certificate. Set
`BRIDGE_CERT_FINGERPRINT` in `.env` to the new certificate's fingerprint
first ([setup](setup.md#4-set-up-the-proton-mail-bridge-app), step 4.3):
a rotation accepts only the certificate matching it. To accept the new
cert, recreate `mbsync` once with `BRIDGE_CERT_PIN_ROTATE=true`:

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
that already exists. While it stays `true`, every cert change that
still matches `BRIDGE_CERT_FINGERPRINT` is accepted without comparison
to the pin. As soon as the rotation has succeeded,
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
for `up`). The next start is then a first boot and pins the cert Bridge
presents once it matches `BRIDGE_CERT_FINGERPRINT`, as a rotation would.

`make clean` removes the `mbsync-state` volume along with everything
else, so the next boot after `make clean` is treated as a first boot
and re-pins the cert Bridge presents once it matches
`BRIDGE_CERT_FINGERPRINT`. `make clean` leaves `.secrets/` alone: the
Bridge app keeps its own login on the host, so its IMAP password stays
valid.
