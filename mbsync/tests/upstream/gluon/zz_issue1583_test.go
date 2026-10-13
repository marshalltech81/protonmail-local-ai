// Issue #1583 harness for Gluon 7e800978ab4a9942829eb6116e33ea0278c03e91.
//
// Copied into the pinned Gluon tree's tests/ package by
// mbsync/tests/upstream/run.sh (protonmail-local-ai) and run only inside
// the isolated, offline container that script assembles. It reuses
// Gluon's own test server and fake (Dummy) connector, wraps the
// connector and the literal store to count calls and bytes, and writes
// only allowlisted results: counts, booleans, timings and fixed tokens.
// Identity values (Gluon IDs, header values) are compared in memory and
// never recorded. Upstream logging is discarded.
package tests

import (
	"bufio"
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"math/big"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/ProtonMail/gluon/connector"
	"github.com/ProtonMail/gluon/imap"
	"github.com/ProtonMail/gluon/internal/ids"
	"github.com/ProtonMail/gluon/rfc822"
	"github.com/ProtonMail/gluon/store"
	"github.com/sirupsen/logrus"
	"github.com/stretchr/testify/require"
)

func init() {
	logrus.SetOutput(io.Discard)
	logrus.SetLevel(logrus.PanicLevel)
}

// ---------------------------------------------------------------------------
// Results: allowlisted shapes only.

var (
	i1583Mu      sync.Mutex
	i1583Results = map[string]map[string]any{}
	i1583KeyRE   = regexp.MustCompile(`^[a-z0-9_]{1,64}$`)
	i1583TokRE   = regexp.MustCompile(`^[A-Za-z_]{0,16}$`)
)

func i1583Rec(tb testing.TB, scenario, key string, value any) {
	tb.Helper()

	if !i1583KeyRE.MatchString(scenario) || !i1583KeyRE.MatchString(key) {
		tb.Fatalf("result key not allowlisted")
	}

	switch v := value.(type) {
	case bool, int, int64, uint32:
	case float64:
		value = float64(int64(v*1000)) / 1000
	case string:
		if !i1583TokRE.MatchString(v) {
			tb.Fatalf("result token not allowlisted")
		}
	default:
		tb.Fatalf("result type not allowlisted")
	}

	i1583Mu.Lock()
	defer i1583Mu.Unlock()

	if i1583Results[scenario] == nil {
		i1583Results[scenario] = map[string]any{}
	}

	i1583Results[scenario][key] = value

	dir := os.Getenv("ISSUE1583_OUT")
	if dir == "" {
		return
	}

	b, err := json.MarshalIndent(i1583Results, "", "  ")
	require.NoError(tb, err)
	require.NoError(tb, os.WriteFile(filepath.Join(dir, "gluon.json"), b, 0o600))
}

// ---------------------------------------------------------------------------
// Probe connector: Gluon's Dummy connector with call counters and
// switchable failures.

type i1583Probe struct {
	*connector.Dummy

	mu         sync.Mutex
	calls      map[string]int
	failCreate int // remaining CreateMessage failures; -1 fails every call
	override   func([]byte) []byte
}

func (p *i1583Probe) count(name string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.calls[name]++
}

func (p *i1583Probe) get(name string) int {
	p.mu.Lock()
	defer p.mu.Unlock()

	return p.calls[name]
}

func (p *i1583Probe) reset() {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.calls = map[string]int{}
}

// mutations sums every connector call that would change remote state.
func (p *i1583Probe) mutations() int {
	p.mu.Lock()
	defer p.mu.Unlock()

	n := 0

	for k, v := range p.calls {
		if k != "get_message_literal" {
			n += v
		}
	}

	return n
}

func (p *i1583Probe) GetMessageLiteral(ctx context.Context, id imap.MessageID) ([]byte, error) {
	p.count("get_message_literal")

	l, err := p.Dummy.GetMessageLiteral(ctx, id)
	if err == nil && p.override != nil {
		l = p.override(l)
	}

	return l, err
}

func (p *i1583Probe) CreateMessage(ctx context.Context, c connector.IMAPStateWrite, mbox imap.MailboxID, literal []byte, flags imap.FlagSet, date time.Time) (imap.Message, []byte, error) {
	p.count("create_message")

	p.mu.Lock()
	fail := p.failCreate != 0
	if p.failCreate > 0 {
		p.failCreate--
	}
	p.mu.Unlock()

	if fail {
		return imap.Message{}, nil, fmt.Errorf("i1583: create refused")
	}

	return p.Dummy.CreateMessage(ctx, c, mbox, literal, flags, date)
}

func (p *i1583Probe) AddMessagesToMailbox(ctx context.Context, c connector.IMAPStateWrite, mids []imap.MessageID, mbox imap.MailboxID) error {
	p.count("add_messages")
	return p.Dummy.AddMessagesToMailbox(ctx, c, mids, mbox)
}

func (p *i1583Probe) RemoveMessagesFromMailbox(ctx context.Context, c connector.IMAPStateWrite, mids []imap.MessageID, mbox imap.MailboxID) error {
	p.count("remove_messages")
	return p.Dummy.RemoveMessagesFromMailbox(ctx, c, mids, mbox)
}

func (p *i1583Probe) MoveMessages(ctx context.Context, c connector.IMAPStateWrite, mids []imap.MessageID, from, to imap.MailboxID) (bool, error) {
	p.count("move_messages")
	return p.Dummy.MoveMessages(ctx, c, mids, from, to)
}

func (p *i1583Probe) MarkMessagesSeen(ctx context.Context, c connector.IMAPStateWrite, mids []imap.MessageID, v bool) error {
	p.count("mark_seen")
	return p.Dummy.MarkMessagesSeen(ctx, c, mids, v)
}

func (p *i1583Probe) MarkMessagesFlagged(ctx context.Context, c connector.IMAPStateWrite, mids []imap.MessageID, v bool) error {
	p.count("mark_flagged")
	return p.Dummy.MarkMessagesFlagged(ctx, c, mids, v)
}

func (p *i1583Probe) MarkMessagesForwarded(ctx context.Context, c connector.IMAPStateWrite, mids []imap.MessageID, v bool) error {
	p.count("mark_forwarded")
	return p.Dummy.MarkMessagesForwarded(ctx, c, mids, v)
}

func (p *i1583Probe) CreateMailbox(ctx context.Context, c connector.IMAPStateWrite, name []string) (imap.Mailbox, error) {
	p.count("create_mailbox")
	return p.Dummy.CreateMailbox(ctx, c, name)
}

func (p *i1583Probe) UpdateMailboxName(ctx context.Context, c connector.IMAPStateWrite, mbox imap.MailboxID, name []string) error {
	p.count("rename_mailbox")
	return p.Dummy.UpdateMailboxName(ctx, c, mbox, name)
}

func (p *i1583Probe) DeleteMailbox(ctx context.Context, c connector.IMAPStateWrite, mbox imap.MailboxID) error {
	p.count("delete_mailbox")
	return p.Dummy.DeleteMailbox(ctx, c, mbox)
}

type i1583ProbeBuilder struct {
	failCreate int
}

func (b *i1583ProbeBuilder) New(usernames []string, password []byte, period time.Duration, flags, permFlags, attrs imap.FlagSet) Connector {
	return &i1583Probe{
		Dummy:      connector.NewDummy(usernames, password, period, flags, permFlags, attrs),
		calls:      map[string]int{},
		failCreate: b.failCreate,
	}
}

func i1583ProbeOf(s *testSession) *i1583Probe {
	p, ok := s.conns[s.userIDs["user"]].(*i1583Probe)
	if !ok {
		s.tb.Fatalf("probe connector missing")
	}

	return p
}

// ---------------------------------------------------------------------------
// Probe store: Gluon's on-disk store with Get/Set counters and a cache
// drop through the store's own Delete.

type i1583Store struct {
	store.Store

	gets, misses, getBytes, sets, setBytes atomic.Int64
}

// Get counts a successful read of a full literal (gets, getBytes) apart
// from a miss (an error, such as the literal not being stored).
func (s *i1583Store) Get(id imap.InternalMessageID) ([]byte, error) {
	b, err := s.Store.Get(id)
	if err != nil {
		s.misses.Add(1)
		return b, err
	}

	s.gets.Add(1)
	s.getBytes.Add(int64(len(b)))

	return b, err
}

// i1583CountReader counts bytes as the store consumes them, so the probe
// adds no buffering or copy to Gluon's streaming Set.
type i1583CountReader struct {
	r io.Reader
	n *atomic.Int64
}

func (c i1583CountReader) Read(p []byte) (int, error) {
	n, err := c.r.Read(p)
	c.n.Add(int64(n))

	return n, err
}

func (s *i1583Store) Set(id imap.InternalMessageID, r io.Reader) error {
	s.sets.Add(1)

	return s.Store.Set(id, i1583CountReader{r: r, n: &s.setBytes})
}

func (s *i1583Store) dropAll(tb testing.TB) int {
	idsList, err := s.Store.List()
	require.NoError(tb, err)
	require.NoError(tb, s.Store.Delete(idsList...))

	return len(idsList)
}

// storedOnly returns the store's only literal, read past the counters.
func (s *i1583Store) storedOnly(tb testing.TB) []byte {
	idsList, err := s.Store.List()
	require.NoError(tb, err)
	require.Len(tb, idsList, 1)
	b, err := s.Store.Get(idsList[0])
	require.NoError(tb, err)

	return b
}

func (s *i1583Store) resetCounters() {
	s.gets.Store(0)
	s.misses.Store(0)
	s.getBytes.Store(0)
	s.sets.Store(0)
	s.setBytes.Store(0)
}

type i1583StoreBuilder struct {
	inner store.OnDiskStoreBuilder

	mu     sync.Mutex
	stores []*i1583Store
}

func (b *i1583StoreBuilder) New(dir, userID string, pass []byte) (store.Store, error) {
	s, err := b.inner.New(dir, userID, pass)
	if err != nil {
		return nil, err
	}

	ps := &i1583Store{Store: s}

	b.mu.Lock()
	b.stores = append(b.stores, ps)
	b.mu.Unlock()

	return ps, nil
}

func (b *i1583StoreBuilder) Delete(dir, userID string) error {
	return b.inner.Delete(dir, userID)
}

func (b *i1583StoreBuilder) only(tb testing.TB) *i1583Store {
	b.mu.Lock()
	defer b.mu.Unlock()
	require.Len(tb, b.stores, 1)

	return b.stores[0]
}

// ---------------------------------------------------------------------------
// Raw IMAP client: counts bytes read from the socket and keeps literals.

type i1583Line struct {
	text     string
	literals [][]byte
}

type i1583Raw struct {
	tb   testing.TB
	conn net.Conn
	r    *bufio.Reader
	read atomic.Int64
	tag  int
}

type i1583CountingReader struct {
	c net.Conn
	n *atomic.Int64
}

func (c i1583CountingReader) Read(p []byte) (int, error) {
	n, err := c.c.Read(p)
	c.n.Add(int64(n))

	return n, err
}

func i1583Dial(s *testSession) *i1583Raw {
	conn, err := net.Dial("tcp", s.listener.Addr().String())
	require.NoError(s.tb, err)

	c := &i1583Raw{tb: s.tb, conn: conn}
	c.r = bufio.NewReaderSize(i1583CountingReader{c: conn, n: &c.read}, 1<<16)

	_, err = c.r.ReadString('\n') // greeting
	require.NoError(s.tb, err)

	status, _ := c.cmd(`LOGIN user pass`)
	require.Equal(s.tb, "OK", status)

	return c
}

var i1583LitRE = regexp.MustCompile(`\{(\d+)\}\r\n$`)

func (c *i1583Raw) readLine() i1583Line {
	var (
		sb   strings.Builder
		lits [][]byte
	)

	for {
		part, err := c.r.ReadString('\n')
		require.NoError(c.tb, err)
		sb.WriteString(part)

		m := i1583LitRE.FindStringSubmatch(part)
		if m == nil {
			break
		}

		n, err := strconv.Atoi(m[1])
		require.NoError(c.tb, err)

		buf := make([]byte, n)
		_, err = io.ReadFull(c.r, buf)
		require.NoError(c.tb, err)

		lits = append(lits, buf)
	}

	return i1583Line{text: sb.String(), literals: lits}
}

// cmd sends one command and returns the tagged status word and the
// untagged lines before it.
func (c *i1583Raw) cmd(format string, args ...any) (string, []i1583Line) {
	c.tag++
	tag := fmt.Sprintf("x%d", c.tag)
	_, err := fmt.Fprintf(c.conn, "%s %s\r\n", tag, fmt.Sprintf(format, args...))
	require.NoError(c.tb, err)

	var lines []i1583Line

	for {
		l := c.readLine()
		if strings.HasPrefix(l.text, tag+" ") {
			fields := strings.Fields(l.text)
			if len(fields) < 2 {
				c.tb.Fatalf("short tagged response")
			}

			lines = append(lines, l)

			return fields[1], lines
		}

		lines = append(lines, l)
	}
}

// appendLit sends APPEND with a synchronising literal.
func (c *i1583Raw) appendLit(mbox string, literal []byte) string {
	c.tag++
	tag := fmt.Sprintf("x%d", c.tag)
	_, err := fmt.Fprintf(c.conn, "%s APPEND %q {%d}\r\n", tag, mbox, len(literal))
	require.NoError(c.tb, err)

	l := c.readLine()
	if !strings.HasPrefix(l.text, "+") {
		return strings.Fields(l.text)[1]
	}

	_, err = c.conn.Write(append(append([]byte{}, literal...), '\r', '\n'))
	require.NoError(c.tb, err)

	for {
		l := c.readLine()
		if strings.HasPrefix(l.text, tag+" ") {
			return strings.Fields(l.text)[1]
		}
	}
}

func (c *i1583Raw) close() {
	c.cmd(`LOGOUT`)
	require.NoError(c.tb, c.conn.Close())
}

// statusMessages returns STATUS (MESSAGES) for a mailbox.
func (c *i1583Raw) statusMessages(mbox string) int {
	status, lines := c.cmd(`STATUS %q (MESSAGES)`, mbox)
	require.Equal(c.tb, "OK", status)

	m := regexp.MustCompile(`MESSAGES (\d+)`).FindStringSubmatch(lines[0].text)
	require.NotNil(c.tb, m)

	n, err := strconv.Atoi(m[1])
	require.NoError(c.tb, err)

	return n
}

type i1583Msg struct {
	uid   uint32
	flags string
	body  []byte
}

var (
	i1583UIDRE   = regexp.MustCompile(`UID (\d+)`)
	i1583FlagsRE = regexp.MustCompile(`FLAGS \(([^)]*)\)`)
)

// fetchAll selects (or examines) a mailbox and fetches UID, FLAGS and,
// when item is not empty, one body item, in UID order.
func (c *i1583Raw) fetchAll(verb, mbox, item string) (string, []i1583Msg) {
	status, _ := c.cmd(`%s %q`, verb, mbox)
	if status != "OK" {
		return status, nil
	}

	items := "UID FLAGS"
	if item != "" {
		items += " " + item
	}

	status, lines := c.cmd(`UID FETCH 1:* (%s)`, items)
	if status != "OK" {
		return status, nil
	}

	var out []i1583Msg

	for _, l := range lines {
		if !strings.Contains(l.text, " FETCH ") {
			continue
		}

		var m i1583Msg

		if u := i1583UIDRE.FindStringSubmatch(l.text); u != nil {
			v, err := strconv.ParseUint(u[1], 10, 32)
			require.NoError(c.tb, err)
			m.uid = uint32(v)
		}

		if f := i1583FlagsRE.FindStringSubmatch(l.text); f != nil {
			m.flags = f[1]
		}

		if len(l.literals) > 0 {
			m.body = l.literals[0]
		}

		out = append(out, m)
	}

	sort.Slice(out, func(i, j int) bool { return out[i].uid < out[j].uid })

	return "OK", out
}

// ---------------------------------------------------------------------------
// Literal helpers (synthetic content only).

func i1583Lit(headers [][2]string, body string) []byte {
	var b bytes.Buffer

	for _, h := range headers {
		fmt.Fprintf(&b, "%s: %s\r\n", h[0], h[1])
	}

	b.WriteString("\r\n")
	b.WriteString(body)

	return b.Bytes()
}

func i1583Base(subject string, extra ...[2]string) [][2]string {
	h := [][2]string{
		{"From", "Synthetic Sender <sender@example.com>"},
		{"To", "recipient@example.com"},
		{"Subject", subject},
		{"Date", "Mon, 05 Oct 2026 10:00:00 +0000"},
		{"Message-Id", "<" + subject + "@example.com>"},
	}

	return append(h, extra...)
}

func i1583Multipart(boundary, text string) []byte {
	h := i1583Base("multipart-case",
		[2]string{"MIME-Version", "1.0"},
		[2]string{"Content-Type", `multipart/mixed; boundary="` + boundary + `"`},
	)

	body := "--" + boundary + "\r\n" +
		"Content-Type: text/plain; charset=utf-8\r\n\r\n" +
		text + "\r\n" +
		"--" + boundary + "\r\n" +
		"Content-Type: application/octet-stream\r\n" +
		"Content-Disposition: attachment; filename=\"synthetic.bin\"\r\n" +
		"Content-Transfer-Encoding: base64\r\n\r\n" +
		"c3ludGhldGljIGF0dGFjaG1lbnQ=\r\n" +
		"--" + boundary + "--\r\n"

	return i1583Lit(h, body)
}

// i1583Fields returns the top-level header field names in order.
func i1583Fields(tb testing.TB, literal []byte) []string {
	h, err := rfc822.Parse(literal).ParseHeader()
	require.NoError(tb, err)

	var keys []string

	h.Entries(func(k, _ string) { keys = append(keys, k) })

	return keys
}

func i1583Count(keys []string, name string) int {
	n := 0

	for _, k := range keys {
		if strings.EqualFold(k, name) {
			n++
		}
	}

	return n
}

// i1583WithoutFirstGluonID removes the first X-Pm-Gluon-Id field when it
// is the topmost field, returning ok=false otherwise.
func i1583WithoutFirstGluonID(tb testing.TB, literal []byte) ([]byte, bool) {
	keys := i1583Fields(tb, literal)
	if len(keys) == 0 || !strings.EqualFold(keys[0], ids.InternalIDKey) {
		return nil, false
	}

	idx := bytes.Index(literal, []byte("\r\n"))
	if idx < 0 {
		return nil, false
	}

	return literal[idx+2:], true
}

func i1583GluonID(tb testing.TB, literal []byte) string {
	v, err := rfc822.GetHeaderValue(literal, ids.InternalIDKey)
	require.NoError(tb, err)

	return v
}

func i1583Opts(tb testing.TB, failCreate int) (*serverOptions, *i1583StoreBuilder) {
	sb := &i1583StoreBuilder{}

	return defaultServerOptions(tb,
		withConnectorBuilder(&i1583ProbeBuilder{failCreate: failCreate}),
		withStoreBuilder(sb),
	), sb
}

// ---------------------------------------------------------------------------
// Item 2: recovery deduplication (S7).

func TestIssue1583Item2RecoveryDedup(t *testing.T) {
	const sc = "item2_recovery_dedup"

	opts, _ := i1583Opts(t, -1)

	runServer(t, opts, func(s *testSession) {
		s.mailboxCreated("user", []string{"Target"})

		base := i1583Base("recovery-base", [2]string{"X-Pm-Internal-Id", "synthetic-internal-id-1"})
		variant := func(name, value string) []byte {
			h := make([][2]string, 0, len(base))
			for _, f := range base {
				if strings.EqualFold(f[0], name) {
					f[1] = value
				}
				h = append(h, f)
			}
			return i1583Lit(h, "synthetic body\r\n")
		}

		submitted := []struct {
			name string
			lit  []byte
		}{
			{"original", i1583Lit(base, "synthetic body\r\n")},
			{"exact_repeat", i1583Lit(base, "synthetic body\r\n")},
			{"date_differs", variant("Date", "Tue, 06 Oct 2026 11:00:00 +0000")},
			{"message_id_differs", variant("Message-Id", "<other@example.com>")},
			{"x_pm_internal_id_differs", variant("X-Pm-Internal-Id", "synthetic-internal-id-2")},
			{"x_pm_gluon_id_added", i1583Lit(append([][2]string{{"X-Pm-Gluon-Id", "synthetic-gluon-id"}}, base...), "synthetic body\r\n")},
			{"subject_differs", variant("Subject", "recovery-other")},
			{"body_differs", i1583Lit(base, "other synthetic body\r\n")},
		}

		c := i1583Dial(s)
		defer c.close()

		probe := i1583ProbeOf(s)
		before := 0

		for _, sub := range submitted {
			status := c.appendLit("Target", sub.lit)
			i1583Rec(t, sc, sub.name+"_append_status", status)

			after := c.statusMessages(ids.GluonRecoveryMailboxName)
			i1583Rec(t, sc, sub.name+"_stored", after > before)

			// The row this APPEND added (the highest UID) against this
			// submission's own bytes, not against any submission.
			if after > before {
				_, rows := c.fetchAll("EXAMINE", ids.GluonRecoveryMailboxName, "BODY.PEEK[]")
				require.Len(t, rows, after)
				i1583Rec(t, sc, sub.name+"_stored_bytes_equal_own_submission", bytes.Equal(rows[len(rows)-1].body, sub.lit))
				_, _ = c.cmd(`UNSELECT`)
			}

			before = after
		}

		i1583Rec(t, sc, "attempted", len(submitted))
		i1583Rec(t, sc, "stored", before)
		i1583Rec(t, sc, "target_messages", c.statusMessages("Target"))
		i1583Rec(t, sc, "connector_create_calls", probe.get("create_message"))

		_, msgs := c.fetchAll("EXAMINE", ids.GluonRecoveryMailboxName, "BODY.PEEK[]")
		require.NotEmpty(t, msgs)

		first := msgs[0].body
		i1583Rec(t, sc, "first_stored_bytes_equal_submitted", bytes.Equal(first, submitted[0].lit))
		keys := i1583Fields(t, first)
		i1583Rec(t, sc, "first_stored_x_pm_gluon_id_fields", i1583Count(keys, "X-Pm-Gluon-Id"))
		i1583Rec(t, sc, "first_stored_x_pm_internal_id_fields", i1583Count(keys, "X-Pm-Internal-Id"))

		equalToSome := 0
		for _, m := range msgs {
			for _, sub := range submitted {
				if bytes.Equal(m.body, sub.lit) {
					equalToSome++
					break
				}
			}
		}

		i1583Rec(t, sc, "stored_equal_to_a_submission", equalToSome)
	})
}

// ---------------------------------------------------------------------------
// Item 3: recovery cache loss, and COPY versus MOVE out of recovery (S8).

func TestIssue1583Item3RecoveryCacheLoss(t *testing.T) {
	const sc = "item3_recovery_cache_loss"

	opts, sb := i1583Opts(t, -1)

	runServer(t, opts, func(s *testSession) {
		s.mailboxCreated("user", []string{"Target"})

		c := i1583Dial(s)
		defer c.close()

		require.Equal(t, "NO", c.appendLit("Target", i1583Lit(i1583Base("cache-loss"), "synthetic body\r\n")))
		i1583Rec(t, sc, "recovered", c.statusMessages(ids.GluonRecoveryMailboxName))

		st := sb.only(t)
		i1583Rec(t, sc, "store_entries_dropped", st.dropAll(t))

		probe := i1583ProbeOf(s)
		probe.reset()

		status, _ := c.cmd(`EXAMINE %q`, ids.GluonRecoveryMailboxName)
		require.Equal(t, "OK", status)

		status, _ = c.cmd(`UID FETCH 1:* (BODY.PEEK[])`)
		i1583Rec(t, sc, "fetch_status", status)
		i1583Rec(t, sc, "connector_literal_fetches", probe.get("get_message_literal"))
		i1583Rec(t, sc, "recovered_after", c.statusMessages(ids.GluonRecoveryMailboxName))
	})
}

func i1583RecoveryOut(t *testing.T, verb string) {
	sc := "item3_recovery_" + strings.ToLower(verb)

	opts, _ := i1583Opts(t, 1)

	runServer(t, opts, func(s *testSession) {
		s.mailboxCreated("user", []string{"Target"})

		submitted := i1583Lit(i1583Base("recovery-out", [2]string{"X-Pm-Internal-Id", "synthetic-internal-id-1"}), "synthetic body\r\n")

		c := i1583Dial(s)
		defer c.close()

		require.Equal(t, "NO", c.appendLit("Target", submitted))
		i1583Rec(t, sc, "recovered_before", c.statusMessages(ids.GluonRecoveryMailboxName))

		probe := i1583ProbeOf(s)
		probe.reset()

		status, _ := c.cmd(`SELECT %q`, ids.GluonRecoveryMailboxName)
		require.Equal(t, "OK", status)

		status, _ = c.cmd(`UID %s 1:* "Target"`, verb)
		i1583Rec(t, sc, "status", status)

		_, _ = c.cmd(`UNSELECT`)

		i1583Rec(t, sc, "recovered_after", c.statusMessages(ids.GluonRecoveryMailboxName))
		i1583Rec(t, sc, "target_after", c.statusMessages("Target"))
		i1583Rec(t, sc, "connector_create_calls", probe.get("create_message"))
		i1583Rec(t, sc, "connector_mutation_calls", probe.mutations())

		_, msgs := c.fetchAll("EXAMINE", "Target", "BODY.PEEK[]")
		require.Len(t, msgs, 1)

		keys := i1583Fields(t, msgs[0].body)
		i1583Rec(t, sc, "target_x_pm_gluon_id_fields", i1583Count(keys, "X-Pm-Gluon-Id"))
		i1583Rec(t, sc, "target_gluon_id_topmost", len(keys) > 0 && strings.EqualFold(keys[0], "X-Pm-Gluon-Id"))

		rest, ok := i1583WithoutFirstGluonID(t, msgs[0].body)
		i1583Rec(t, sc, "target_bytes_equal_submitted_after_gluon_id", ok && bytes.Equal(rest, submitted))

		if verb == "COPY" {
			_, rmsgs := c.fetchAll("EXAMINE", ids.GluonRecoveryMailboxName, "BODY.PEEK[]")
			require.Len(t, rmsgs, 1)
			i1583Rec(t, sc, "recovery_bytes_still_equal_submitted", bytes.Equal(rmsgs[0].body, submitted))
		}
	})
}

func TestIssue1583Item3RecoveryCopy(t *testing.T) { i1583RecoveryOut(t, "COPY") }
func TestIssue1583Item3RecoveryMove(t *testing.T) { i1583RecoveryOut(t, "MOVE") }

// ---------------------------------------------------------------------------
// Item 4: the APPEND identity hint (S9).

func TestIssue1583Item4AppendHint(t *testing.T) {
	const sc = "item4_append_hint"

	opts, _ := i1583Opts(t, 0)

	runServer(t, opts, func(s *testSession) {
		src := s.mailboxCreated("user", []string{"Source"})
		s.mailboxCreated("user", []string{"Other"})
		s.mailboxCreated("user", []string{"Unknown"})
		s.mailboxCreatedWithAttributes("user", []string{"Drafts"}, imap.NewFlagSet(imap.AttrDrafts))
		s.messageCreated("user", src, i1583Lit(i1583Base("hint-source"), "source body\r\n"), time.Now())

		c := i1583Dial(s)
		defer c.close()

		_, srcMsgs := c.fetchAll("EXAMINE", "Source", "BODY.PEEK[]")
		require.Len(t, srcMsgs, 1)

		existing := srcMsgs[0].body
		existingID := i1583GluonID(t, existing)
		require.NotEmpty(t, existingID)

		// Different subject and body, carrying the existing message's Gluon ID.
		hinted := i1583Lit(append([][2]string{{"X-Pm-Gluon-Id", existingID}}, i1583Base("hint-different")...), "different body\r\n")

		probe := i1583ProbeOf(s)

		cases := []struct {
			name, mbox string
			lit        []byte
		}{
			{"regular", "Other", hinted},
			{"drafts", "Drafts", hinted},
			{"unknown_id", "Unknown", i1583Lit(append([][2]string{{"X-Pm-Gluon-Id", imap.NewInternalMessageID().String()}}, i1583Base("hint-unknown")...), "unknown body\r\n")},
		}

		for _, cs := range cases {
			probe.reset()

			i1583Rec(t, sc, cs.name+"_append_status", c.appendLit(cs.mbox, cs.lit))
			i1583Rec(t, sc, cs.name+"_connector_create_calls", probe.get("create_message"))
			i1583Rec(t, sc, cs.name+"_connector_add_calls", probe.get("add_messages"))

			_, msgs := c.fetchAll("EXAMINE", cs.mbox, "BODY.PEEK[]")
			i1583Rec(t, sc, cs.name+"_messages", len(msgs))

			if len(msgs) != 1 {
				continue
			}

			got := msgs[0].body
			i1583Rec(t, sc, cs.name+"_bytes_equal_existing", bytes.Equal(got, existing))
			i1583Rec(t, sc, cs.name+"_bytes_equal_appended", bytes.Equal(got, cs.lit))
			i1583Rec(t, sc, cs.name+"_gluon_id_equal_existing", i1583GluonID(t, got) == existingID)
			i1583Rec(t, sc, cs.name+"_x_pm_gluon_id_fields", i1583Count(i1583Fields(t, got), "X-Pm-Gluon-Id"))
		}

		i1583Rec(t, sc, "source_messages_after", c.statusMessages("Source"))
	})
}

// ---------------------------------------------------------------------------
// Item 5: rebuild paths (S5, S8): cache refill and equivalent updates.

func TestIssue1583Item5Rebuild(t *testing.T) {
	const sc = "item5_rebuild"

	opts, sb := i1583Opts(t, 0)

	runServer(t, opts, func(s *testSession) {
		mbox := s.mailboxCreated("user", []string{"Box"})
		original := i1583Multipart("boundary-one", "synthetic text")
		mid := imap.MessageID("i1583-message")
		s.messageCreatedWithID("user", mid, mbox, original, time.Now())

		probe := i1583ProbeOf(s)
		st := sb.only(t)

		observe := func() i1583Msg {
			c := i1583Dial(s)
			defer c.close()

			_, msgs := c.fetchAll("EXAMINE", "Box", "BODY.PEEK[]")
			require.Len(t, msgs, 1)

			return msgs[0]
		}

		m0 := observe()

		// a. Refill after cache loss, connector returns the same bytes.
		st.dropAll(t)
		probe.reset()
		m1 := observe()
		i1583Rec(t, sc, "refill_same_connector_fetches", probe.get("get_message_literal"))
		i1583Rec(t, sc, "refill_same_uid_unchanged", m1.uid == m0.uid)
		i1583Rec(t, sc, "refill_same_bytes_equal", bytes.Equal(m1.body, m0.body))

		// b. Refill after cache loss, connector rebuilds with another boundary (S3).
		probe.override = func([]byte) []byte { return i1583Multipart("boundary-two", "synthetic text") }
		st.dropAll(t)
		probe.reset()
		m2 := observe()
		probe.override = nil
		i1583Rec(t, sc, "refill_rebuilt_connector_fetches", probe.get("get_message_literal"))
		i1583Rec(t, sc, "refill_rebuilt_uid_unchanged", m2.uid == m0.uid)
		i1583Rec(t, sc, "refill_rebuilt_bytes_equal", bytes.Equal(m2.body, m0.body))
		i1583Rec(t, sc, "refill_rebuilt_gluon_id_unchanged", i1583GluonID(t, m2.body) == i1583GluonID(t, m0.body))
		i1583Rec(t, sc, "refill_rebuilt_x_pm_gluon_id_fields", i1583Count(i1583Fields(t, m2.body), "X-Pm-Gluon-Id"))

		// c. Equivalent update (another boundary) with the literal cached.
		before := observe()
		s.messageUpdatedWithID("user", mid, mbox, i1583Multipart("boundary-three", "synthetic text"), time.Now())
		m3 := observe()
		i1583Rec(t, sc, "equivalent_cached_uid_unchanged", m3.uid == before.uid)
		i1583Rec(t, sc, "equivalent_cached_bytes_equal", bytes.Equal(m3.body, before.body))
		i1583Rec(t, sc, "equivalent_cached_gluon_id_unchanged", i1583GluonID(t, m3.body) == i1583GluonID(t, before.body))

		// d. Equivalent update while the literal is not cached.
		before = observe()
		st.dropAll(t)
		s.messageUpdatedWithID("user", mid, mbox, i1583Multipart("boundary-four", "synthetic text"), time.Now())
		m4 := observe()
		i1583Rec(t, sc, "equivalent_uncached_uid_unchanged", m4.uid == before.uid)
		i1583Rec(t, sc, "equivalent_uncached_bytes_equal", bytes.Equal(m4.body, before.body))
		i1583Rec(t, sc, "equivalent_uncached_gluon_id_unchanged", i1583GluonID(t, m4.body) == i1583GluonID(t, before.body))

		// e. The connector repeats the bytes it sent last, literal cached.
		// They carry no X-Pm-Gluon-Id, which Gluon adds before comparing,
		// so they are not the stored bytes.
		before = observe()
		s.messageUpdatedWithID("user", mid, mbox, i1583Multipart("boundary-four", "synthetic text"), time.Now())
		m5 := observe()
		i1583Rec(t, sc, "repeat_cached_uid_unchanged", m5.uid == before.uid)
		i1583Rec(t, sc, "repeat_cached_bytes_equal", bytes.Equal(m5.body, before.body))

		// f. Byte-identical update, literal cached: exactly the stored
		// literal, Gluon's X-Pm-Gluon-Id included.
		stored := st.storedOnly(t)
		before = observe()
		s.messageUpdatedWithID("user", mid, mbox, stored, time.Now())
		m6 := observe()
		i1583Rec(t, sc, "identical_cached_input_equals_served", bytes.Equal(stored, before.body))
		i1583Rec(t, sc, "identical_cached_uid_unchanged", m6.uid == before.uid)
		i1583Rec(t, sc, "identical_cached_bytes_equal", bytes.Equal(m6.body, before.body))
		i1583Rec(t, sc, "identical_cached_gluon_id_unchanged", i1583GluonID(t, m6.body) == i1583GluonID(t, before.body))

		// g. Content update (control).
		before = observe()
		s.messageUpdatedWithID("user", mid, mbox, i1583Multipart("boundary-four", "changed synthetic text"), time.Now())
		m7 := observe()
		i1583Rec(t, sc, "changed_uid_unchanged", m7.uid == before.uid)
		i1583Rec(t, sc, "changed_gluon_id_unchanged", i1583GluonID(t, m7.body) == i1583GluonID(t, before.body))
	})
}

// ---------------------------------------------------------------------------
// Item 6: flag scope (S11) and the file names isync writes.

// i1583Cert makes a synthetic self-signed certificate for 127.0.0.1 with
// a common name: isync refuses Gluon's test certificate, which has none.
func i1583Cert(t *testing.T) (tls.Certificate, []byte) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	require.NoError(t, err)

	tmpl := &x509.Certificate{
		SerialNumber:          big.NewInt(1583),
		Subject:               pkix.Name{CommonName: "127.0.0.1"},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(time.Hour),
		KeyUsage:              x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		BasicConstraintsValid: true,
		IsCA:                  true,
		IPAddresses:           []net.IP{net.ParseIP("127.0.0.1")},
	}

	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	require.NoError(t, err)

	return tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key}, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
}

func TestIssue1583Item6FlagScope(t *testing.T) {
	const sc = "item6_flag_scope"

	cert, certPEM := i1583Cert(t)
	saved := testCert
	testCert = cert
	t.Cleanup(func() { testCert = saved })

	opts, _ := i1583Opts(t, 0)

	runServer(t, opts, func(s *testSession) {
		a := s.mailboxCreated("user", []string{"Alpha"})
		b := s.mailboxCreated("user", []string{"Beta"})
		s.messageCreatedWithMailboxes("user", []imap.MailboxID{a, b}, i1583Lit(i1583Base("flag-scope"), "synthetic body\r\n"), time.Now())

		c := i1583Dial(s)

		status, _ := c.cmd(`SELECT "Alpha"`)
		require.Equal(t, "OK", status)
		status, _ = c.cmd(`UID STORE 1:* +FLAGS (\Seen \Flagged \Deleted)`)
		i1583Rec(t, sc, "store_status", status)
		_, _ = c.cmd(`UNSELECT`)

		for _, mb := range []string{"Alpha", "Beta"} {
			_, msgs := c.fetchAll("EXAMINE", mb, "")
			require.Len(t, msgs, 1)
			p := strings.ToLower(mb)
			i1583Rec(t, sc, p+"_imap_seen", strings.Contains(msgs[0].flags, `\Seen`))
			i1583Rec(t, sc, p+"_imap_flagged", strings.Contains(msgs[0].flags, `\Flagged`))
			i1583Rec(t, sc, p+"_imap_deleted", strings.Contains(msgs[0].flags, `\Deleted`))
			_, _ = c.cmd(`UNSELECT`)
		}

		c.close()

		maildir := i1583RunMbsync(t, sc, s, certPEM, []string{"Alpha", "Beta"})

		for _, mb := range []string{"Alpha", "Beta"} {
			p := strings.ToLower(mb)
			files := i1583MaildirFiles(t, filepath.Join(maildir, mb))
			i1583Rec(t, sc, p+"_maildir_files", len(files))

			for _, f := range files {
				i1583Rec(t, sc, p+"_maildir_subdir", f.subdir)
				i1583Rec(t, sc, p+"_maildir_flags", f.flags)
			}
		}
	})
}

type i1583File struct {
	subdir, flags string
	data          []byte
}

func i1583MaildirFiles(tb testing.TB, dir string) []i1583File {
	var out []i1583File

	for _, sub := range []string{"new", "cur"} {
		entries, err := os.ReadDir(filepath.Join(dir, sub))
		if os.IsNotExist(err) {
			continue
		}

		require.NoError(tb, err)

		for _, e := range entries {
			name := e.Name()
			flags := ""

			if i := strings.LastIndex(name, ":2,"); i >= 0 {
				flags = name[i+3:]
			}

			data, err := os.ReadFile(filepath.Join(dir, sub, name))
			require.NoError(tb, err)

			out = append(out, i1583File{subdir: sub, flags: flags, data: data})
		}
	}

	return out
}

// i1583RunMbsync runs the image's isync against the in-process Gluon
// server (STARTTLS on the container's own loopback; the shipped
// configuration uses implicit TLS through a Tunnel, which this harness
// does not need). Only the exit status is recorded.
func i1583RunMbsync(t *testing.T, sc string, s *testSession, certPEM []byte, mailboxes []string) string {
	host, port, err := net.SplitHostPort(s.listener.Addr().String())
	require.NoError(t, err)
	i1583Rec(t, sc, "listener_ipv4_loopback", host == "127.0.0.1")

	work := t.TempDir()
	maildir := filepath.Join(work, "maildir")
	require.NoError(t, os.MkdirAll(maildir, 0o700))
	require.NoError(t, os.WriteFile(filepath.Join(work, "cert.pem"), certPEM, 0o600))
	require.NoError(t, os.WriteFile(filepath.Join(work, "pass"), []byte("pass"), 0o600))

	quoted := make([]string, len(mailboxes))
	for i, m := range mailboxes {
		quoted[i] = strconv.Quote(m)
	}

	cfg := strings.Join([]string{
		"IMAPAccount acc",
		"Host " + host,
		"Port " + port,
		"User user",
		`PassCmd "cat ` + filepath.Join(work, "pass") + `"`,
		"TLSType STARTTLS",
		"CertificateFile " + filepath.Join(work, "cert.pem"),
		"",
		"IMAPStore remote",
		"Account acc",
		"",
		"MaildirStore local",
		"Path " + maildir + "/",
		"Inbox " + maildir + "/INBOX",
		"SubFolders Legacy",
		"",
		"Channel ch",
		"Far :remote:",
		"Near :local:",
		"Patterns " + strings.Join(quoted, " "),
		"Create Near",
		"SyncState *",
		"Sync Pull",
		"Expunge None",
		"CopyArrivalDate yes",
		"",
	}, "\n")
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

// ---------------------------------------------------------------------------
// Item 7: read-only commands (S10).

func TestIssue1583Item7ReadOnly(t *testing.T) {
	const sc = "item7_read_only"

	opts, _ := i1583Opts(t, 0)

	runServer(t, opts, func(s *testSession) {
		mbox := s.mailboxCreated("user", []string{"Box"})
		for i := range 3 {
			s.messageCreated("user", mbox, i1583Lit(i1583Base(fmt.Sprintf("read-only-%d", i), [2]string{"X-Pm-Internal-Id", "synthetic"}), "synthetic body\r\n"), time.Now())
		}

		c := i1583Dial(s)
		defer c.close()

		// Setup: one message marked \Deleted in a writable selection.
		status, _ := c.cmd(`SELECT "Box"`)
		require.Equal(t, "OK", status)
		status, _ = c.cmd(`UID STORE 3 +FLAGS (\Deleted)`)
		require.Equal(t, "OK", status)
		_, _ = c.cmd(`UNSELECT`)

		probe := i1583ProbeOf(s)
		probe.reset()

		status, lines := c.cmd(`EXAMINE "Box"`)
		i1583Rec(t, sc, "examine_status", status)
		i1583Rec(t, sc, "examine_read_only", strings.Contains(lines[len(lines)-1].text, "[READ-ONLY]"))

		status, _ = c.cmd(`UID FETCH 1:* (BODY.PEEK[HEADER.FIELDS (X-Pm-Internal-Id)])`)
		i1583Rec(t, sc, "peek_status", status)

		status, flagLines := c.cmd(`UID FETCH 1:* (FLAGS)`)
		require.Equal(t, "OK", status)

		seen, deleted := 0, 0
		for _, l := range flagLines {
			if strings.Contains(l.text, `\Seen`) {
				seen++
			}
			if strings.Contains(l.text, `\Deleted`) {
				deleted++
			}
		}

		i1583Rec(t, sc, "seen_after_peek", seen)
		i1583Rec(t, sc, "deleted_after_peek", deleted)

		status, _ = c.cmd(`UID STORE 1 +FLAGS (\Seen)`)
		i1583Rec(t, sc, "store_under_examine_status", status)

		status, _ = c.cmd(`UNSELECT`)
		i1583Rec(t, sc, "unselect_status", status)
		i1583Rec(t, sc, "messages_after_unselect", c.statusMessages("Box"))
		i1583Rec(t, sc, "connector_mutations_read_only_path", probe.mutations())
		i1583Rec(t, sc, "connector_literal_fetches_read_only_path", probe.get("get_message_literal"))

		status, _ = c.cmd(`EXAMINE "Box"`)
		require.Equal(t, "OK", status)
		status, _ = c.cmd(`CLOSE`)
		i1583Rec(t, sc, "close_read_only_status", status)
		i1583Rec(t, sc, "messages_after_read_only_close", c.statusMessages("Box"))
		i1583Rec(t, sc, "connector_mutations_after_read_only_close", probe.mutations())

		status, _ = c.cmd(`SELECT "Box"`)
		require.Equal(t, "OK", status)
		status, _ = c.cmd(`CLOSE`)
		i1583Rec(t, sc, "close_writable_status", status)
		i1583Rec(t, sc, "messages_after_writable_close", c.statusMessages("Box"))
		i1583Rec(t, sc, "connector_remove_calls_after_writable_close", probe.get("remove_messages"))
	})
}

// ---------------------------------------------------------------------------
// Item 8: header-only workload (S10), warm and cold.

func i1583ReadProcStatus(key string) (int64, bool) {
	b, err := os.ReadFile("/proc/self/status")
	if err != nil {
		return 0, false
	}

	for _, line := range strings.Split(string(b), "\n") {
		if strings.HasPrefix(line, key+":") {
			f := strings.Fields(line)
			if len(f) >= 2 {
				v, err := strconv.ParseInt(f[1], 10, 64)
				return v, err == nil
			}
		}
	}

	return 0, false
}

func TestIssue1583Item8HeaderOnly(t *testing.T) {
	const (
		sc       = "item8_header_only"
		messages = 200
		bodySize = 256 * 1024
	)

	opts, sb := i1583Opts(t, 0)

	runServer(t, opts, func(s *testSession) {
		mbox := s.mailboxCreated("user", []string{"Label"})
		line := strings.Repeat("y", 76) + "\r\n"
		body := strings.Repeat(line, bodySize/len(line))

		var literalBytes int64

		s.batchMessageCreated("user", mbox, messages, func(i int) ([]byte, []string) {
			l := i1583Lit(i1583Base(fmt.Sprintf("workload-%d", i), [2]string{"X-Pm-Internal-Id", fmt.Sprintf("synthetic-%d", i)}), body)
			literalBytes += int64(len(l))
			return l, nil
		})

		i1583Rec(t, sc, "messages", messages)
		i1583Rec(t, sc, "connector_literal_bytes", literalBytes)

		probe := i1583ProbeOf(s)
		st := sb.only(t)

		run := func(phase string) {
			c := i1583Dial(s)
			defer c.close()

			status, _ := c.cmd(`EXAMINE "Label"`)
			require.Equal(t, "OK", status)

			probe.reset()
			st.resetCounters()

			hwmReset := os.WriteFile("/proc/self/clear_refs", []byte("5"), 0o600) == nil
			rssBefore, _ := i1583ReadProcStatus("VmRSS")
			readBefore := c.read.Load()
			start := time.Now()

			status, lines := c.cmd(`UID FETCH 1:* (UID BODY.PEEK[HEADER.FIELDS (X-Pm-Internal-Id)])`)
			elapsed := time.Since(start)
			downstream := c.read.Load() - readBefore
			hwm, _ := i1583ReadProcStatus("VmHWM")

			require.Equal(t, "OK", status)

			fetched, literalBytesDown := 0, 0
			for _, l := range lines {
				if strings.Contains(l.text, " FETCH ") {
					fetched++
					for _, lit := range l.literals {
						literalBytesDown += len(lit)
					}
				}
			}

			i1583Rec(t, sc, phase+"_fetch_responses", fetched)
			i1583Rec(t, sc, phase+"_downstream_bytes", downstream)
			i1583Rec(t, sc, phase+"_downstream_literal_bytes", literalBytesDown)
			i1583Rec(t, sc, phase+"_store_full_literal_reads", st.gets.Load())
			i1583Rec(t, sc, phase+"_store_misses", st.misses.Load())
			i1583Rec(t, sc, phase+"_store_full_literal_bytes", st.getBytes.Load())
			i1583Rec(t, sc, phase+"_store_writes", st.sets.Load())
			i1583Rec(t, sc, phase+"_store_write_bytes", st.setBytes.Load())
			i1583Rec(t, sc, phase+"_connector_literal_fetches", probe.get("get_message_literal"))
			i1583Rec(t, sc, phase+"_connector_mutations", probe.mutations())
			i1583Rec(t, sc, phase+"_fetch_ms", float64(elapsed.Microseconds())/1000)
			i1583Rec(t, sc, phase+"_rss_peak_reset", hwmReset)
			i1583Rec(t, sc, phase+"_rss_before_kib", rssBefore)
			i1583Rec(t, sc, phase+"_rss_peak_kib", hwm)

			status, flagLines := c.cmd(`UID FETCH 1:* (FLAGS)`)
			require.Equal(t, "OK", status)

			seen := 0
			for _, l := range flagLines {
				if strings.Contains(l.text, `\Seen`) {
					seen++
				}
			}

			i1583Rec(t, sc, phase+"_seen_after", seen)

			status, _ = c.cmd(`UNSELECT`)
			require.Equal(t, "OK", status)
		}

		run("warm")
		i1583Rec(t, sc, "cold_store_entries_dropped", st.dropAll(t))
		run("cold")
		run("rewarm")
	})
}
