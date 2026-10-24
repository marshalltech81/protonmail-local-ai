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
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/ProtonMail/gluon/rfc822"
	"github.com/ProtonMail/go-proton-api"
	"github.com/ProtonMail/go-proton-api/server"
	"github.com/ProtonMail/proton-bridge/v3/internal/bridge"
	"github.com/emersion/go-imap"
	"github.com/emersion/go-imap/client"
	"github.com/sirupsen/logrus"
	"github.com/stretchr/testify/require"
	"go.uber.org/mock/gomock"
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

// Issue #1664 (H11): APPEND through Bridge's own IMAP connector, against
// the go-proton-api fake server. Every literal is synthetic; IDs are
// compared in memory and only counts and booleans are recorded.

const (
	i1664Internal = "synthetic-forged-internal"
	// Well-formed (a UUID), so Gluon's APPEND takes its Gluon-ID path;
	// no stored message has it.
	i1664Gluon = "5c1f0d0e-0000-4000-8000-000000000011"
	// Not a UUID: Gluon's APPEND refuses it before reaching the connector.
	i1664GluonBad = "synthetic-forged-gluon"
)

// i1664Literal builds a synthetic literal from sender, with the given
// extra header lines first (so an identity field can sit above the rest).
func i1664Literal(from, subject string, extra ...string) []byte {
	var b bytes.Buffer

	for _, h := range extra {
		b.WriteString(h + "\r\n")
	}

	b.WriteString("From: " + from + "\r\n")
	b.WriteString("To: recipient@example.com\r\n")
	b.WriteString("Subject: " + subject + "\r\n")
	b.WriteString("Date: Mon, 05 Oct 2026 10:00:00 +0000\r\n")
	b.WriteString("Message-Id: <" + subject + "@example.com>\r\n")
	b.WriteString("Content-Type: text/plain; charset=utf-8\r\n")
	b.WriteString("\r\nsynthetic body\r\n")

	return b.Bytes()
}

// i1664Mailbox returns a mailbox's literals keyed by Subject, and whether
// it could be selected.
func i1664Mailbox(tb testing.TB, cl *client.Client, name string) (map[string][]byte, bool) {
	msgs, err := clientFetch(cl, name)
	if err != nil {
		return nil, false
	}

	section, err := imap.ParseBodySectionName("BODY[]")
	require.NoError(tb, err)

	out := map[string][]byte{}

	for _, m := range msgs {
		body := m.GetBody(section)
		require.NotNil(tb, body)

		lit, err := io.ReadAll(body)
		require.NoError(tb, err)

		_, subject := i1583ShapeOf(tb, lit, nil)
		out[subject] = lit
	}

	return out, true
}

func i1664Listed(tb testing.TB, cl *client.Client, name string) bool {
	ch := make(chan *imap.MailboxInfo, 64)
	done := make(chan error, 1)

	go func() { done <- cl.List("", name, ch) }()

	n := 0
	for range ch {
		n++
	}

	require.NoError(tb, <-done)

	return n > 0
}

func i1664APIIDs(ctx context.Context, tb testing.TB, s *server.Server) map[string]bool {
	ids := map[string]bool{}

	m := proton.New(proton.WithHostURL(s.GetHostURL()), proton.WithTransport(proton.InsecureTransport()))
	c, _, err := m.NewClientWithLogin(ctx, username, password)
	require.NoError(tb, err)

	defer c.Close()

	metas, err := c.GetMessageMetadata(ctx, proton.MessageFilter{})
	require.NoError(tb, err)

	for _, md := range metas {
		ids[md.ID] = true
	}

	return ids
}

// i1664Served records every shape key for one stored literal (or zeros
// when it is absent), so each scenario has the same keys every run.
func i1664Served(tb testing.TB, sc, p string, lit []byte, present bool, submitted []byte, newIDs map[string]bool) {
	var (
		sh i1583Shape
	)

	if present {
		sh, _ = i1583ShapeOf(tb, lit, newIDs)
	}

	i1583Rec(tb, sc, p+"_present", present)
	i1583Rec(tb, sc, p+"_x_pm_internal_id_fields", sh.internal)
	i1583Rec(tb, sc, p+"_x_pm_internal_id_equal_new_proton_id", sh.internalEqual)
	i1583Rec(tb, sc, p+"_x_pm_gluon_id_fields", sh.gluon)
	i1583Rec(tb, sc, p+"_gluon_id_topmost", sh.gluonTopmost)
	i1583Rec(tb, sc, p+"_contains_forged_internal", present && bytes.Contains(lit, []byte(i1664Internal)))
	i1583Rec(tb, sc, p+"_contains_forged_gluon", present &&
		(bytes.Contains(lit, []byte(i1664Gluon)) || bytes.Contains(lit, []byte(i1664GluonBad))))
	i1583Rec(tb, sc, p+"_bytes_equal_submitted", present && bytes.Equal(lit, submitted))
}

func TestIssue1583Item11ConnectorAppend(t *testing.T) {
	logrus.SetOutput(io.Discard)
	logrus.SetLevel(logrus.PanicLevel)

	const sc = "item11_connector_append"

	var (
		failImport     atomic.Bool
		importAttempts atomic.Int64
		draftCreates   atomic.Int64
	)

	withEnv(t, func(ctx context.Context, s *server.Server, netCtl *proton.NetCtl, locator bridge.Locator, storeKey []byte) {
		// Counts every import and draft-create request, and refuses the
		// import while failImport is set.
		s.AddStatusHook(func(r *http.Request) (int, bool) {
			if r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/mail/v4/messages/import") {
				importAttempts.Add(1)

				if failImport.Load() {
					return http.StatusUnprocessableEntity, true
				}
			}

			// A metadata listing is a POST with a method override; a draft
			// create is a plain POST.
			if r.Method == http.MethodPost && r.Header.Get("X-HTTP-Method-Override") == "" &&
				strings.HasSuffix(r.URL.Path, "/mail/v4/messages") {
				draftCreates.Add(1)
			}

			return 0, false
		})

		withClient(ctx, t, s, username, password, func(ctx context.Context, c *proton.Client) {
			addrs, err := c.GetAddresses(ctx)
			require.NoError(t, err)

			ids := createMessages(ctx, t, c, addrs[0].ID, proton.InboxLabel,
				i1583Literal("existing"))
			require.Len(t, ids, 1)
		})

		withBridgeWaitForServers(ctx, t, s.GetHostURL(), netCtl, locator, storeKey, func(b *bridge.Bridge, mocks *bridge.Mocks) {
			// A refused APPEND is reported by Bridge; this test provokes them.
			mocks.Reporter.EXPECT().ReportMessageWithContext(gomock.Any(), gomock.Any()).AnyTimes()

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

			pool := x509.NewCertPool()
			require.True(t, pool.AppendCertsFromPEM(certPEM))

			cl, err := client.DialTLS(addr, &tls.Config{RootCAs: pool, ServerName: "127.0.0.1", MinVersion: tls.VersionTLS12})
			require.NoError(t, err)
			require.NoError(t, cl.Login(info.Addresses[0], string(info.BridgePass)))

			defer func() { _ = cl.Logout() }()

			from := info.Addresses[0]
			sender := "Synthetic Sender <sender@example.com>"

			inbox, ok := i1664Mailbox(t, cl, "INBOX")
			require.True(t, ok)
			existing, found := inbox["existing"]
			require.True(t, found)

			existingGluon, err := rfc822.GetHeaderValue(existing, "X-Pm-Gluon-Id")
			require.NoError(t, err)
			require.NotEmpty(t, existingGluon)

			// appendCase appends one literal, then records what changed on
			// both sides. It returns the new Proton IDs.
			appendCase := func(name, mailbox string, lit []byte) (map[string]bool, bool) {
				idsBefore := i1664APIIDs(ctx, t, s)
				attemptsBefore, draftsBefore := importAttempts.Load(), draftCreates.Load()

				before, _ := i1664Mailbox(t, cl, mailbox)
				err := cl.Append(mailbox, nil, time.Now(), bytes.NewReader(lit))
				after, _ := i1664Mailbox(t, cl, mailbox)

				idsAfter := i1664APIIDs(ctx, t, s)
				newIDs := map[string]bool{}

				for id := range idsAfter {
					if !idsBefore[id] {
						newIDs[id] = true
					}
				}

				i1583Rec(t, sc, name+"_append_accepted", err == nil)
				i1583Rec(t, sc, name+"_import_requests", int(importAttempts.Load()-attemptsBefore))
				i1583Rec(t, sc, name+"_draft_create_requests", int(draftCreates.Load()-draftsBefore))
				i1583Rec(t, sc, name+"_new_proton_messages", len(newIDs))
				i1583Rec(t, sc, name+"_mailbox_messages_before", len(before))
				i1583Rec(t, sc, name+"_mailbox_messages_after", len(after))

				return newIDs, err == nil
			}

			// (a) A successful APPEND of a literal carrying forged
			// X-Pm-Internal-Id and a well-formed unknown X-Pm-Gluon-Id.
			subjA := "a-forged-both"
			litA := i1664Literal(sender, subjA, "X-Pm-Internal-Id: "+i1664Internal, "X-Pm-Gluon-Id: "+i1664Gluon)
			newA, _ := appendCase("a", "INBOX", litA)
			servedA, _ := i1664Mailbox(t, cl, "INBOX")
			gotA, presentA := servedA[subjA]
			i1664Served(t, sc, "a_served", gotA, presentA, litA, newA)

			// (a2) The same with a Gluon ID that is not a UUID: Gluon
			// refuses it before the connector, so it is recovered.
			subjA2 := "a2-malformed-gluon"
			litA2 := i1664Literal(sender, subjA2, "X-Pm-Internal-Id: "+i1664Internal, "X-Pm-Gluon-Id: "+i1664GluonBad)
			newA2, _ := appendCase("a2", "INBOX", litA2)
			servedA2, _ := i1664Mailbox(t, cl, "INBOX")
			gotA2, presentA2 := servedA2[subjA2]
			i1664Served(t, sc, "a2_served_in_inbox", gotA2, presentA2, litA2, newA2)

			// (b) The connector's import refused by the server.
			failImport.Store(true)

			subjB := "b-import-refused"
			litB := i1664Literal(sender, subjB, "X-Pm-Internal-Id: "+i1664Internal, "X-Pm-Gluon-Id: "+i1664Gluon)
			newB, _ := appendCase("b", "INBOX", litB)

			failImport.Store(false)

			servedBInbox, _ := i1664Mailbox(t, cl, "INBOX")
			gotBInbox, presentBInbox := servedBInbox[subjB]
			i1664Served(t, sc, "b_served_in_inbox", gotBInbox, presentBInbox, litB, newB)

			// Recovery: what the failed APPENDs (a2 and b) left in
			// "Recovered Messages".
			i1583Rec(t, sc, "recovered_mailbox_listed", i1664Listed(t, cl, "Recovered Messages"))

			recovered, recSelected := i1664Mailbox(t, cl, "Recovered Messages")
			i1583Rec(t, sc, "recovered_mailbox_selected", recSelected)
			i1583Rec(t, sc, "recovered_messages", len(recovered))

			gotRecA2, presentRecA2 := recovered[subjA2]
			i1664Served(t, sc, "a2_recovered", gotRecA2, presentRecA2, litA2, nil)

			gotRecB, presentRecB := recovered[subjB]
			i1664Served(t, sc, "b_recovered", gotRecB, presentRecB, litB, nil)

			// (c) Drafts: a control whose Gluon ID Gluon can erase, and a
			// literal whose header Gluon cannot parse (a bare CR in a
			// value above the Gluon ID), so its erase fails.
			subjC0 := "c0-drafts-erasable"
			litC0 := i1664Literal(from, subjC0, "X-Pm-Internal-Id: "+i1664Internal, "X-Pm-Gluon-Id: "+i1664Gluon)
			_, erasedErr0 := rfc822.EraseHeaderValue(litC0, "X-Pm-Gluon-Id")
			i1583Rec(t, sc, "c0_erase_function_fails", erasedErr0 != nil)

			newC0, _ := appendCase("c0", "Drafts", litC0)
			draftsC0, _ := i1664Mailbox(t, cl, "Drafts")
			gotC0, presentC0 := draftsC0[subjC0]
			i1664Served(t, sc, "c0_served", gotC0, presentC0, litC0, newC0)

			subjC1 := "c1-drafts-erase-fails"
			litC1 := i1664Literal(from, subjC1, "X-Bad: a\rb", "X-Pm-Internal-Id: "+i1664Internal, "X-Pm-Gluon-Id: "+i1664Gluon)
			_, erasedErr1 := rfc822.EraseHeaderValue(litC1, "X-Pm-Gluon-Id")
			i1583Rec(t, sc, "c1_erase_function_fails", erasedErr1 != nil)

			newC1, _ := appendCase("c1", "Drafts", litC1)
			draftsC1, _ := i1664Mailbox(t, cl, "Drafts")
			gotC1, presentC1 := draftsC1[subjC1]
			i1664Served(t, sc, "c1_served", gotC1, presentC1, litC1, newC1)

			// Recovery again, after the Drafts cases: did the refused c1
			// APPEND leave a literal there?
			recoveredLater, _ := i1664Mailbox(t, cl, "Recovered Messages")
			i1583Rec(t, sc, "recovered_messages_after_drafts", len(recoveredLater))

			// The literal's header does not parse, so it is not keyed by a
			// Subject: it is found by its bytes.
			var gotRecC1 []byte

			presentRecC1 := false

			for _, lit := range recoveredLater {
				if bytes.Equal(lit, litC1) {
					gotRecC1, presentRecC1 = lit, true
				}
			}

			i1664Served(t, sc, "c1_recovered", gotRecC1, presentRecC1, litC1, nil)

			// (d) An APPEND naming an existing message's Gluon ID (to a
			// mailbox that does not hold it yet).
			subjD := "d-existing-gluon-id"
			litD := i1664Literal(sender, subjD, "X-Pm-Internal-Id: "+i1664Internal, "X-Pm-Gluon-Id: "+existingGluon)
			newD, _ := appendCase("d", "Archive", litD)
			archive, _ := i1664Mailbox(t, cl, "Archive")

			gotDExisting, presentDExisting := archive["existing"]
			_, presentDSubmitted := archive[subjD]

			i1583Rec(t, sc, "d_archive_holds_existing_message", presentDExisting)
			i1583Rec(t, sc, "d_archive_holds_submitted_subject", presentDSubmitted)
			i1583Rec(t, sc, "d_archive_message_bytes_equal_existing_inbox_bytes", presentDExisting && bytes.Equal(gotDExisting, existing))
			i1583Rec(t, sc, "d_new_proton_ids_seen", len(newD))
		})
	})
}
