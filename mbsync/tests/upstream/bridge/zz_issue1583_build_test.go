// Issue #1583 harness for Bridge b9c5dac1651437100c40896dacd778a0518a26f2.
//
// Copied into internal/services/imapservice by
// mbsync/tests/upstream/run.sh (protonmail-local-ai) and run only inside
// the isolated, offline container that script assembles. It builds one
// synthetic Proton message with Bridge's own builder and the job options
// its IMAP sync uses (defaultMessageJobOpts), varying only the
// X-Pm-Internal-Id fields already present in the message's parsed
// headers (S1). Only counts and booleans are recorded; the IDs are
// compared in memory.
package imapservice

import (
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/ProtonMail/gluon/rfc822"
	"github.com/ProtonMail/go-proton-api"
	"github.com/ProtonMail/gopenpgp/v2/crypto"
	"github.com/ProtonMail/proton-bridge/v3/pkg/message"
	"github.com/ProtonMail/proton-bridge/v3/utils"
	"github.com/sirupsen/logrus"
	"github.com/stretchr/testify/require"
)

var (
	i1583BuildResults = map[string]map[string]any{}
	i1583BuildKeyRE   = regexp.MustCompile(`^[a-z0-9_]{1,64}$`)
)

func i1583BuildRec(tb testing.TB, scenario, key string, value any) {
	tb.Helper()

	if !i1583BuildKeyRE.MatchString(scenario) || !i1583BuildKeyRE.MatchString(key) {
		tb.Fatalf("result key not allowlisted")
	}

	switch value.(type) {
	case bool, int:
	default:
		tb.Fatalf("result type not allowlisted")
	}

	if i1583BuildResults[scenario] == nil {
		i1583BuildResults[scenario] = map[string]any{}
	}

	i1583BuildResults[scenario][key] = value

	if dir := os.Getenv("ISSUE1583_OUT"); dir != "" {
		b, err := json.MarshalIndent(i1583BuildResults, "", "  ")
		require.NoError(tb, err)
		require.NoError(tb, os.WriteFile(filepath.Join(dir, "bridge-builder.json"), b, 0o600))
	}
}

func TestIssue1583Item1Builder(t *testing.T) {
	logrus.SetOutput(io.Discard)
	logrus.SetLevel(logrus.PanicLevel)

	const (
		sc        = "item1_builder"
		protonID  = "synthetic-proton-id"
		forgedID  = "synthetic-forged-id"
		forgedID2 = "synthetic-forged-id-2"
	)

	kr := utils.MakeKeyRing(t)

	enc, err := kr.Encrypt(crypto.NewPlainMessageFromString("synthetic body"), kr)
	require.NoError(t, err)

	armored, err := enc.GetArmored()
	require.NoError(t, err)

	date := time.Date(2026, 10, 5, 10, 0, 0, 0, time.UTC)

	cases := []struct {
		name   string
		values []string
	}{
		{"none", nil},
		{"one_equal", []string{protonID}},
		{"one_forged", []string{forgedID}},
		{"equal_then_forged", []string{protonID, forgedID}},
		{"forged_then_equal", []string{forgedID, protonID}},
		{"two_forged", []string{forgedID, forgedID2}},
	}

	for _, cs := range cases {
		hdr := proton.Headers{
			Values: map[string][]string{
				"Content-Type": {"text/plain"},
				"Date":         {date.Format(time.RFC1123Z)},
				"Subject":      {"synthetic"},
			},
			Order: []string{"Content-Type", "Date", "Subject"},
		}

		if cs.values != nil {
			hdr.Values["X-Pm-Internal-Id"] = cs.values
			hdr.Order = append(hdr.Order, "X-Pm-Internal-Id")
		}

		msg := proton.Message{
			MessageMetadata: proton.MessageMetadata{
				ID:        protonID,
				AddressID: "synthetic-address-id",
				Time:      date.Unix(),
				Subject:   "synthetic",
			},
			ParsedHeaders: hdr,
			MIMEType:      "text/plain",
			Body:          armored,
		}

		built, err := message.DecryptAndBuildRFC822(kr, msg, nil, defaultMessageJobOpts())
		require.NoError(t, err)

		h, err := rfc822.Parse(built).ParseHeader()
		require.NoError(t, err)

		var values []string

		h.Entries(func(k, v string) {
			if strings.EqualFold(k, "X-Pm-Internal-Id") {
				values = append(values, strings.TrimSpace(v))
			}
		})

		equal, forged := 0, 0

		for _, v := range values {
			if v == protonID {
				equal++
			} else {
				forged++
			}
		}

		i1583BuildRec(t, sc, cs.name+"_input_fields", len(cs.values))
		i1583BuildRec(t, sc, cs.name+"_input_topmost_equal", len(cs.values) > 0 && cs.values[0] == protonID)
		i1583BuildRec(t, sc, cs.name+"_output_fields", len(values))
		i1583BuildRec(t, sc, cs.name+"_output_fields_equal_id", equal)
		i1583BuildRec(t, sc, cs.name+"_output_fields_other", forged)
		i1583BuildRec(t, sc, cs.name+"_output_first_equal_id", len(values) > 0 && values[0] == protonID)
	}
}
