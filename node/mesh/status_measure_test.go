package mesh

import (
	"encoding/json"
	"strings"
	"testing"
	"time"

	"openbear/node/mesh/health"
)

// The UDP socket is bound to every address: status shows the one the
// system sends from to the peer, on the socket's port.
func TestLocalAddrOfTheWildcardSocket(t *testing.T) {
	for _, c := range [][3]string{
		{"[::]:51820", "127.0.0.1:9", "127.0.0.1:51820"},
		{"0.0.0.0:51820", "127.0.0.1:9", "127.0.0.1:51820"},
		{"[::]:51820", "[::ffff:127.0.0.1]:9", "127.0.0.1:51820"},
		{"10.0.0.5:40112", "127.0.0.1:9", "10.0.0.5:40112"}, // a stream's own
		{"[::]:51820", "", "[::]:51820"},                    // no address to look up
		{"", "127.0.0.1:9", ""},
	} {
		if got := localAddrOf(c[0], c[1]); got != c[2] {
			t.Errorf("localAddrOf(%q, %q) = %q, want %q", c[0], c[1], got, c[2])
		}
	}
}

// entry.candidates[]: what was measured, null for what was not.
func TestCandidateMeasurementFields(t *testing.T) {
	c := &Config{Entry: &Entry{Mode: "failover", Candidates: []Candidate{{ID: "c1", Relay: "A", Endpoint: "udp://192.0.2.1:1"}, {ID: "c2", Relay: "B", Endpoint: "tcp://192.0.2.2:1"}, {ID: "c3", Relay: "C", Endpoint: "tcp://192.0.2.3:1"}}}}
	at := time.UnixMilli(1793186711968)
	hs := health.Snapshot{Candidates: []health.CandidateStatus{
		{ID: "c1", Measure: health.Measurement{Probes: 100, Timeouts: 3, CheckedAt: at, RTT: 1500 * time.Microsecond, Loss: .05, Recent: 60}},
		{ID: "c2", Measure: health.Measurement{Probes: 4, Timeouts: 4, CheckedAt: at, Loss: 1, Recent: 1}}, // nothing answered
		{ID: "c3"}, // never checked
	}}
	raw, err := json.Marshal(candidateHealth(c, hs))
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{
		`"id":"c1","relay":"A","transport":"udp","state":"untested","since":0,"lastOkAt":0,"lastError":"","via":"","rttMs":1.5,"lossRate":0.05,"checkedAt":1793186711968,"probes":100,"timeouts":3`,
		`"id":"c2","relay":"B","transport":"tcp","state":"untested","since":0,"lastOkAt":0,"lastError":"","via":"","rttMs":null,"lossRate":1,"checkedAt":1793186711968,"probes":4,"timeouts":4`,
		`"id":"c3","relay":"C","transport":"tcp","state":"untested","since":0,"lastOkAt":0,"lastError":"","via":"","rttMs":null,"lossRate":null,"checkedAt":0,"probes":0,"timeouts":0`,
	} {
		if !strings.Contains(string(raw), want) {
			t.Errorf("no %s in %s", want, raw)
		}
	}
}
