# evidence-run

Code for the #1584 live evidence run, under procedure v5 (approved by the
owner 2026-10-24, posted on #1584). It is **not** part of any image or
service. It needs no account, no Bridge, no Tart and no network to test:
`make test-evidence-run` runs everything here against synthetic data and
local responders on the loopback interface.

Nothing here has touched Proton. The run itself happens later, in an
isolated guest, after the owner clears the procedure's gates.

| Piece | What it does | What its tests prove | What they do not prove |
|---|---|---|---|
| `probe.py` | One in-memory pass per delivered file: the indexer's parser, `Evidence` built as the indexer builds it, `assess()`; counts per reason, parser tokens, equality of candidates by a keyed digest that never leaves the process. | The probe's `Evidence` equals the indexer's own read of the stored rows for each policy shape; every policy branch (including `headers_unknown`, `headers_incomplete`); a really nested message is a counted token. | That Bridge delivers any of these shapes. |
| `export.py` | The only way anything leaves the run: counts, booleans, timings and tokens from a fixed vocabulary. | Anything off the allowlist is refused; a canary in the subject, addresses, body, Message-ID, folder and Proton ID never reaches the export. | That a future new key is safe: adding one is a reviewed change to the allowlist. |
| `p11.py` | The P11 comparison: three fixed-token counts, then a digest of the file with its one `X-TUID` removed and CRLF restored against a digest of the raw served bytes. | It reports a difference for a MIME-boundary-only change, a one-header change and a body change, and a failed precondition for a second `X-TUID`, a tuid or bare line ending in the served bytes; equality only for the documented difference. | That native isync on macOS equals the Debian isync of H3: version-equal, not build-equal. |
| `tlscapture.py` | Looks at the server certificate, compares its SHA-256 with the pinned one **before any credential**, reconnects trusting only that certificate with hostname verification, then `EXAMINE` and `BODY.PEEK`. | A wrong certificate is refused with no `LOGIN` sent; a certificate swapped between the two connections is refused before `LOGIN`; no write command is ever sent; sizes are bounded before bodies are fetched; the password file must be mode 600 and the password is never in an error. | Behaviour against Bridge itself (IMAP quirks, its certificate). |
| `syncdriver.py` | Runs each isync run itself, bounded to 600 s and 1 MiB of output, killing the process group; output is counted, never kept. | Timeout and output-cap kills (the process is gone), exit codes, a missing binary, each run counted as a command. | A real isync run (no provider is contacted here). |
| `render_isync_config.sh` | The repository's own `render_mbsync_config` render, then five exact line replacements (Tunnel to `Port`, secret file, certificate file, Maildir and its INBOX). | The result differs from `mbsync/tests/mbsyncrc.golden` in exactly those five lines; pull-only, `Expunge None`, `TLSType IMAPS` kept; unsafe values refused before anything is written. | That isync accepts the file: only the guest runs isync. Needs `envsubst` (gettext). |
| `sender.py` | stdlib `smtplib` over implicit TLS with a verified certificate; raw bytes sent as given. | Duplicate and forged fields arrive as submitted; an untrusted certificate is refused before `AUTH`; a drop after `DATA` is an ambiguous outcome, a refused recipient a refusal. | Any real provider's behaviour. |
| `fixtures.py` | Plain, multipart and forged/duplicated/conflicting `X-Pm-*` messages (CRLF only, at most 64 KiB, tokens `SYNTH-run-n`) and the G6 set (200 x 256 KiB), generated on demand and never kept. | Sizes are exact and capped, the fields are as designed, every fixture parses. | That Proton accepts or preserves them. |
| `isolation.py` | The guest's isolation matrix: IPv4 and IPv6 x TCP, UDP, ICMP, positive controls and blocked targets; a report of counts and a verdict. | The pass/fail logic (a leaked block target, a failed control, a missing cell are each a failure), against local responders. | That the guest's network is isolated: only the guest can say; the exact `ping` flags are confirmed there. |
| `runner.py` | Budgets and stop rules: attempts (retries and ambiguous outcomes count), pacing at 10 per second, unique versus transfer bytes, group clocks sized from worst-case waits, first failed prediction or unexplained message stops. | Each limit with an injected clock; the frozen service-message list. | The real waits of a live run. |

| `frozen/` and `frozen.py` | The inputs fixed before the first account action, as committed data: predictions P1 to P12 and each G7 step's expectation (`predictions.json`), the fixture plan with each group's batches and operations (`fixtures.json`), and the expected Proton service messages (`service_messages.json`, a placeholder until the owner freezes it). `bundle_hash` covers all three; the owner's approval is a mode-600 file holding it. | The loader refuses a malformed set; the service list is refused until marked frozen with the hash of its rules, and an edit after freezing breaks it; every group clock is sized from worst-case waits and is under the ceiling; the approval binds to exactly these inputs (a changed prediction or count invalidates it), is refused when absent, loose, a symlink or wrong, and live also needs the passing account-free stage report. | That the service-message list is right: it is a placeholder here. |
| `world.py`, `simulated.py` | The port the driver talks to, and an in-memory world that models what the procedure *predicts* of Bridge (delivery rebuilds one Internal ID and one Gluon ID, drops forged fields; moves change a UID; a sync writes the isync-shaped copy), with a scenario that flips each behaviour. | Every prediction can hold, fail, or be unobservable under the driver; each stop rule fires. | Anything about Bridge or Proton: the simulation is the procedure's expectation, not evidence. |
| `pieces.py` | The world made of the real pieces: the smtplib sender, the pinned-TLS capture (with UIDVALIDITY and flags) joined to the files isync wrote (read only), the supervised isync, the isolation matrix, and the C route check (the same forged message sent to the second address and read there). The steps only the owner or the guest can do go to an operator world and are refused without one. | Against local responders and a stub isync: a send goes out as given, served literals join the Maildir files by correlation token, the Maildir is never written, group one runs end to end, a missing operator is a fixed stop, a copy that never arrives is "not checkable". | A real Bridge, isync, Gmail or isolation; the operator world is not in this PR. |
| `stage_report.py` | Turns `isolation.py`'s output and the TLS pin check into the account-free stage report the live guard requires: two booleans, as an allowlisted export, written mode 600; the exit code says whether it passes. | A failed matrix or an unchecked pin gives a report the guard refuses; a passing one is accepted with the frozen list and the approval; input that is not the matrix output is refused. | That the matrix ran in the guest: only the guest produces its input. |
| `driver.py` | G1 to G8 in order in one process, from the frozen inputs; every cross-step comparison in memory; G7's steps in the fixed order with a baseline captured immediately before each; the stop rules, each a fixed token in the export's `stop` section; `--dry-run` (simulated world, virtual clock) and a guarded `--live`. | The frozen-list refusal; a plan size or run id the fixture generator cannot build is refused at construction, before any account activity (one number per decimal width of each numbering, so the work does not grow with counts), and one that still fails mid-run stops as `fixture_unbuildable`; every stop rule (first failed prediction, unexplained message, foreign party, provider warning, failed isolation test, exhausted clock or budget, convergence timeout, lost probe state, a restart the process cannot span, a declined operator step, an identifier that would reach the export); each prediction failing closed; the budgets (attempts with retries and ambiguous outcomes, unique versus transfer bytes); nothing written but the allowlisted export (mode 600); a canary. | That the live groups work: `--live` stops with `live_world_not_configured` after the guard, because the guest wires the live world. |

The group clock ceiling of 3600 s is the procedure's earlier 60-minute
group limit, kept as a guard: a group whose worst-case waits exceed it is
refused at start.

## Running the driver

```bash
# from evidence-run/: offline, the simulated world, no accounts
export PYTHONPATH=.:../indexer
uv run --project ../indexer --python 3.14 python -m evidence_run.driver --dry-run --export /path/to/export.json

# values the owner records before the account stage
uv run --project ../indexer --python 3.14 python -m evidence_run.driver --service-hash      # goes in service_messages.json when frozen
uv run --project ../indexer --python 3.14 python -m evidence_run.driver --print-bundle-hash # goes in the owner's approval file (mode 600)
```

`--live` runs nothing until the service list is frozen, the approval file
holds the bundle hash, and the account-free stage report passes. The
approval file and the report are never committed.

P11 pairs a file with its message by correlation token, so it compares
only messages whose token is unique in the folder; G5's identical
appends, which share one token by design, are left out of it. Every eligible
message needs both retained and fresh file evidence; a missing file or an
empty comparison is unobservable. G7 compares every tracked token, and G8
checks identity before and after the spam move plus all recorded UID
ownership history. A disproved prediction stops subsequent operations
immediately. G1's initial and incremental batches follow the frozen counts.

The fixture addresses (`sender.example`, `mail.example`) are placeholders;
the run's real From and To addresses are a configuration the live world
supplies, and `allowed_domains` in `fixtures.json` must name them.

Correlation reads one unambiguous top-level Subject and From using the
standard-library header parser; quoted body fields never qualify a message.
Header bytes, field count, and correlation-field lengths are bounded
before parsing. Missing or malformed evidence cannot certify a test sender.
Header-shape checks decode only the identity fields, leaving unrelated
structured address headers unparsed. G3 and G4 separately export counts of
equal, changed and unavailable Gluon-ID comparisons; identifiers and their
digests stay in memory. A missing, blank or duplicate Gluon field makes that
experiment unobservable. Gluon changes are reported independently of the
Proton-ID prediction, without assuming both identifiers share a lifecycle.

P11 fresh files must come from the operator-world interface with a new
empty destination and independent sync state for every comparison. The
configured fresh paths alone cannot prove that isolation and are not used;
without the interface, P11 is unobservable (#1783). An untriggered H11
expectation is also unobservable unless its frozen expectation is itself
`no_trigger`. Isolation is reported passed only after a successful check.
