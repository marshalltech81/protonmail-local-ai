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

The group clock ceiling of 3600 s is the procedure's earlier 60-minute
group limit, kept as a guard: a group whose worst-case waits exceed it is
refused at start.
