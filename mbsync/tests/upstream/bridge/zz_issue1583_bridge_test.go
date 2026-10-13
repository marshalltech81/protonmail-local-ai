// Issue #1583 harness for Bridge b9c5dac1651437100c40896dacd778a0518a26f2.
//
// Copied into internal/bridge by mbsync/tests/upstream/run.sh
// (protonmail-local-ai) and run only inside the isolated, offline
// container that script assembles. It uses Bridge's own integration
// setup: the go-proton-api fake server, a Bridge instance logged in to a
// synthetic user, Bridge's IMAP server in implicit-TLS mode on the
// container's loopback, and the image's isync syncing INBOX into a
// Maildir with the shipped configuration's sync settings. It records
// which identity header shapes reach the Maildir files. Only counts and
// booleans are recorded; IDs are compared in memory.
package bridge_test

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"testing"

	"github.com/ProtonMail/gluon/rfc822"
	"github.com/ProtonMail/go-proton-api"
	"github.com/ProtonMail/go-proton-api/server"
	"github.com/ProtonMail/proton-bridge/v3/internal/bridge"
	"github.com/emersion/go-imap"
	"github.com/emersion/go-imap/client"
	"github.com/sirupsen/logrus"
	"github.com/stretchr/testify/require"
)

var (
	i1583Results = map[string]map[string]any{}
	i1583KeyRE   = regexp.MustCompile(`^[a-z0-9_]{1,64}$`)
)

func i1583Rec(tb testing.TB, scenario, key string, value any) {
	tb.Helper()

	if !i1583KeyRE.MatchString(scenario) || !i1583KeyRE.MatchString(key) {
		tb.Fatalf("result key not allowlisted")
	}

	switch value.(type) {
	case bool, int:
	default:
		tb.Fatalf("result type not allowlisted")
	}

	if i1583Results[scenario] == nil {
		i1583Results[scenario] = map[string]any{}
	}

	i1583Results[scenario][key] = value

	if dir := os.Getenv("ISSUE1583_OUT"); dir != "" {
		b, err := json.MarshalIndent(i1583Results, "", "  ")
		require.NoError(tb, err)
		require.NoError(tb, os.WriteFile(filepath.Join(dir, "bridge.json"), b, 0o600))
	}
}

func i1583Literal(subject string, extra ...string) []byte {
	var b bytes.Buffer

	for _, h := range extra {
		b.WriteString(h + "\r\n")
	}

	b.WriteString("From: Synthetic Sender <sender@example.com>\r\n")
	b.WriteString("To: recipient@example.com\r\n")
	b.WriteString("Subject: " + subject + "\r\n")
	b.WriteString("Date: Mon, 05 Oct 2026 10:00:00 +0000\r\n")
	b.WriteString("Message-Id: <" + subject + "@example.com>\r\n")
	b.WriteString("Content-Type: text/plain; charset=utf-8\r\n")
	b.WriteString("\r\nsynthetic body\r\n")

	return b.Bytes()
}

type i1583Shape struct {
	internal, internalEqual, gluon int
	gluonTopmost                   bool
}

func i1583ShapeOf(tb testing.TB, literal []byte, protonIDs map[string]bool) (i1583Shape, string) {
	h, err := rfc822.Parse(literal).ParseHeader()
	require.NoError(tb, err)

	var (
		s       i1583Shape
		first   = true
		subject string
	)

	h.Entries(func(k, v string) {
		switch {
		case strings.EqualFold(k, "X-Pm-Internal-Id"):
			s.internal++
			if protonIDs[strings.TrimSpace(v)] {
				s.internalEqual++
			}
		case strings.EqualFold(k, "X-Pm-Gluon-Id"):
			s.gluon++
			if first {
				s.gluonTopmost = true
			}
		case strings.EqualFold(k, "Subject"):
			subject = strings.TrimSpace(v)
		}

		first = false
	})

	return s, subject
}

func i1583Keys(tb testing.TB, literal []byte) []string {
	h, err := rfc822.Parse(literal).ParseHeader()
	require.NoError(tb, err)

	var keys []string

	h.Entries(func(k, _ string) { keys = append(keys, k) })

	return keys
}

// i1583Inbox reads a Maildir INBOX's files keyed by their (synthetic)
// Subject.
func i1583Inbox(tb testing.TB, maildir string, protonIDs map[string]bool) map[string][]byte {
	out := map[string][]byte{}

	for _, sub := range []string{"new", "cur"} {
		entries, err := os.ReadDir(filepath.Join(maildir, "INBOX", sub))
		if os.IsNotExist(err) {
			continue
		}

		require.NoError(tb, err)

		for _, e := range entries {
			data, err := os.ReadFile(filepath.Join(maildir, "INBOX", sub, e.Name()))
			require.NoError(tb, err)

			_, subject := i1583ShapeOf(tb, data, protonIDs)
			out[subject] = data
		}
	}

	return out
}

func TestIssue1583Item1FakeAPIDelivery(t *testing.T) {
	logrus.SetOutput(io.Discard)
	logrus.SetLevel(logrus.PanicLevel)

	const sc = "item1_fake_api_delivery"

	cases := map[string][]byte{
		"case-plain":           i1583Literal("case-plain"),
		"case-forged-internal": i1583Literal("case-forged-internal", "X-Pm-Internal-Id: synthetic-forged-internal"),
		"case-two-forged":      i1583Literal("case-two-forged", "X-Pm-Internal-Id: synthetic-forged-a", "X-Pm-Internal-Id: synthetic-forged-b"),
		"case-forged-gluon":    i1583Literal("case-forged-gluon", "X-Pm-Gluon-Id: synthetic-forged-gluon"),
		"case-forged-both":     i1583Literal("case-forged-both", "X-Pm-Gluon-Id: synthetic-forged-gluon", "X-Pm-Internal-Id: synthetic-forged-internal"),
	}

	withEnv(t, func(ctx context.Context, s *server.Server, netCtl *proton.NetCtl, locator bridge.Locator, storeKey []byte) {
		// Each case is imported on its own, so its Proton ID is known and a
		// file is compared with its own message's ID, not any imported one.
		idBySubject := map[string]string{}
		protonIDs := map[string]bool{}

		withClient(ctx, t, s, username, password, func(ctx context.Context, c *proton.Client) {
			addrs, err := c.GetAddresses(ctx)
			require.NoError(t, err)

			for subject, l := range cases {
				ids := createMessages(ctx, t, c, addrs[0].ID, proton.InboxLabel, l)
				require.Len(t, ids, 1)
				idBySubject[subject] = ids[0]
				protonIDs[ids[0]] = true
			}
		})

		i1583Rec(t, sc, "imported", len(protonIDs))

		withBridgeWaitForServers(ctx, t, s.GetHostURL(), netCtl, locator, storeKey, func(b *bridge.Bridge, _ *bridge.Mocks) {
			ready := waitForIMAPServerReady(b)
			require.NoError(t, b.SetIMAPSSL(ctx, true))
			ready.Wait()
			ready.Done()

			userLoginAndSync(ctx, t, b, username, password)

			userIDs := b.GetUserIDs()
			require.Len(t, userIDs, 1)

			info, err := b.GetUserInfo(userIDs[0])
			require.NoError(t, err)

			certPEM, _ := b.GetBridgeTLSCert()
			addr := net.JoinHostPort("127.0.0.1", strconv.Itoa(b.GetIMAPPort()))

			// What Bridge serves over IMAP, for comparison with the files.
			pool := x509.NewCertPool()
			require.True(t, pool.AppendCertsFromPEM(certPEM))

			cl, err := client.DialTLS(addr, &tls.Config{RootCAs: pool, ServerName: "127.0.0.1", MinVersion: tls.VersionTLS12})
			require.NoError(t, err)
			require.NoError(t, cl.Login(info.Addresses[0], string(info.BridgePass)))

			msgs, err := clientFetch(cl, "INBOX")
			require.NoError(t, err)
			require.NoError(t, cl.Logout())

			section, err := imap.ParseBodySectionName("BODY[]")
			require.NoError(t, err)

			served := map[string][]byte{}

			for _, m := range msgs {
				body := m.GetBody(section)
				require.NotNil(t, body)

				lit, err := io.ReadAll(body)
				require.NoError(t, err)

				_, subject := i1583ShapeOf(t, lit, protonIDs)
				served[subject] = lit
			}

			i1583Rec(t, sc, "imap_inbox_messages", len(msgs))

			maildir := i1583Mbsync(t, sc, addr, info.Addresses[0], string(info.BridgePass), certPEM)

			files := 0

			for _, sub := range []string{"new", "cur"} {
				dir := filepath.Join(maildir, "INBOX", sub)

				entries, err := os.ReadDir(dir)
				if os.IsNotExist(err) {
					continue
				}

				require.NoError(t, err)

				for _, e := range entries {
					data, err := os.ReadFile(filepath.Join(dir, e.Name()))
					require.NoError(t, err)

					files++

					_, subject := i1583ShapeOf(t, data, nil)
					shape, _ := i1583ShapeOf(t, data, map[string]bool{idBySubject[subject]: true})
					orig, ok := cases[subject]
					if !ok {
						i1583Rec(t, sc, "file_without_known_case", true)
						continue
					}

					p := strings.ReplaceAll(strings.TrimPrefix(subject, "case-"), "-", "_")
					in, _ := i1583ShapeOf(t, orig, protonIDs)

					i1583Rec(t, sc, p+"_input_x_pm_internal_id_fields", in.internal)
					i1583Rec(t, sc, p+"_input_x_pm_gluon_id_fields", in.gluon)
					i1583Rec(t, sc, p+"_file_x_pm_internal_id_fields", shape.internal)
					i1583Rec(t, sc, p+"_file_x_pm_internal_id_equal_proton_id", shape.internalEqual)
					i1583Rec(t, sc, p+"_file_x_pm_gluon_id_fields", shape.gluon)
					i1583Rec(t, sc, p+"_file_gluon_id_topmost", shape.gluonTopmost)
					i1583Rec(t, sc, p+"_file_contains_forged_marker", bytes.Contains(data, []byte("synthetic-forged")))

					imapLit, ok := served[subject]
					require.True(t, ok)

					lf := func(b []byte) []byte { return bytes.ReplaceAll(b, []byte("\r\n"), []byte("\n")) }
					fileHdr, fileBody := rfc822.Split(data)
					imapHdr, imapBody := rfc822.Split(imapLit)
					fileKeys, imapKeys := i1583Keys(t, data), i1583Keys(t, imapLit)

					i1583Rec(t, sc, p+"_file_bytes_equal_imap", bytes.Equal(data, imapLit))
					i1583Rec(t, sc, p+"_file_bytes_equal_imap_crlf_to_lf", bytes.Equal(lf(data), lf(imapLit)))
					i1583Rec(t, sc, p+"_file_body_equal_imap_crlf_to_lf", bytes.Equal(lf(fileBody), lf(imapBody)))
					i1583Rec(t, sc, p+"_file_header_equal_imap_crlf_to_lf", bytes.Equal(lf(fileHdr), lf(imapHdr)))
					i1583Rec(t, sc, p+"_file_header_fields", len(fileKeys))
					i1583Rec(t, sc, p+"_imap_header_fields", len(imapKeys))
					i1583Rec(t, sc, p+"_file_header_names_equal_imap", strings.Join(fileKeys, "\n") == strings.Join(imapKeys, "\n"))
					tuid := 0
					for _, k := range fileKeys {
						if strings.EqualFold(k, "X-TUID") {
							tuid++
						}
					}

					i1583Rec(t, sc, p+"_file_x_tuid_fields", tuid)
					i1583Rec(t, sc, p+"_file_equal_imap_crlf_to_lf_without_x_tuid", bytes.Equal(
						regexp.MustCompile(`(?m)^X-TUID: [^\n]*\n`).ReplaceAll(lf(data), nil), lf(imapLit)))
					i1583Rec(t, sc, p+"_file_crlf_lines", bytes.Count(data, []byte("\r\n")))
					i1583Rec(t, sc, p+"_imap_crlf_lines", bytes.Count(imapLit, []byte("\r\n")))
				}
			}

			i1583Rec(t, sc, "maildir_files", files)

			// A second sync of the same unchanged messages into an empty
			// Maildir: are the files byte-identical to the first sync's?
			first := i1583Inbox(t, maildir, protonIDs)
			second := i1583Inbox(t, i1583Mbsync(t, sc+"_second", addr, info.Addresses[0], string(info.BridgePass), certPEM), protonIDs)
			tuidRE := regexp.MustCompile(`(?m)^X-TUID: [^\n]*\n`)
			equal, equalWithoutTUID := 0, 0

			for subject, data := range first {
				if bytes.Equal(data, second[subject]) {
					equal++
				}

				if bytes.Equal(tuidRE.ReplaceAll(data, nil), tuidRE.ReplaceAll(second[subject], nil)) {
					equalWithoutTUID++
				}
			}

			i1583Rec(t, sc, "second_sync_files", len(second))
			i1583Rec(t, sc, "second_sync_files_byte_equal_first", equal)
			i1583Rec(t, sc, "second_sync_files_equal_first_without_x_tuid", equalWithoutTUID)
		})
	})
}

// i1583Mbsync runs the image's isync against Bridge's IMAP server with
// the shipped configuration's TLS and sync settings (TLSType IMAPS,
// CertificateFile, Host 127.0.0.1, Sync Pull, Expunge None, SubFolders
// Legacy), connecting directly instead of through the Tunnel. Only the
// exit status is recorded.
func i1583Mbsync(t *testing.T, sc, addr, user, pass string, certPEM []byte) string {
	host, port, err := net.SplitHostPort(addr)
	require.NoError(t, err)

	work := t.TempDir()
	maildir := filepath.Join(work, "maildir")
	require.NoError(t, os.MkdirAll(maildir, 0o700))
	require.NoError(t, os.WriteFile(filepath.Join(work, "cert.pem"), certPEM, 0o600))
	require.NoError(t, os.WriteFile(filepath.Join(work, "pass"), []byte(pass), 0o600))

	cfg := fmt.Sprintf(`IMAPAccount acc
Host %s
Port %s
User %s
PassCmd "cat %s"
TLSType IMAPS
CertificateFile %s

IMAPStore remote
Account acc

MaildirStore local
Path %s/
Inbox %s/INBOX
SubFolders Legacy

Channel ch
Far :remote:
Near :local:
Patterns INBOX
Create Near
SyncState *
Sync Pull
Expunge None
CopyArrivalDate yes
`, host, port, user, filepath.Join(work, "pass"), filepath.Join(work, "cert.pem"), maildir, maildir)
	require.NoError(t, os.WriteFile(filepath.Join(work, "mbsyncrc"), []byte(cfg), 0o600))

	cmd := exec.Command("mbsync", "-c", filepath.Join(work, "mbsyncrc"), "-a")
	cmd.Env = []string{"HOME=" + work, "PATH=/usr/bin:/bin"}
	cmd.Stdout = io.Discard
	cmd.Stderr = io.Discard

	// isync's own diagnostics stay in the container's log directory.
	if logs := os.Getenv("ISSUE1583_LOGS"); logs != "" {
		f, ferr := os.Create(filepath.Join(logs, "mbsync-"+sc+".log"))
		require.NoError(t, ferr)
		defer func() { _ = f.Close() }()
		cmd.Stderr = f
	}

	err = cmd.Run()
	i1583Rec(t, sc, "mbsync_exit_zero", err == nil)
	// No Maildir evidence is recorded from a failed sync.
	require.NoError(t, err, "mbsync failed")

	return maildir
}
