package routing

import (
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"testing"
	"time"
)

// decode120 is what a relay of node 1.2.0 makes of m: its decoders as they
// shipped (node-v1.2.0, advert.go), kept here because relays of this version
// meet relays still on it while a mesh is updated. nil: it takes m.
func decode120(m Message, ids map[uint32]string, known map[string]bool) error {
	links := map[string]bool{}
	routes := map[string]bool{}
	start := func(origin string, seq uint64, nLinks, nRoutes int) error {
		if !known[origin] || seq == 0 {
			return errors.New("advert from an unknown relay")
		}
		if nLinks > len(known) || nRoutes > len(known) {
			return errors.New("advert lists more relays than there are")
		}
		return nil
	}
	link := func(origin, peer string, cost float64, state string) error {
		if !known[peer] || peer == origin || state == "" || math.IsNaN(cost) || cost < 0 || cost > maxAdvertCost {
			return errors.New("advert link is invalid")
		}
		if links[peer] {
			return errors.New("advert repeats a link")
		}
		links[peer] = true
		return nil
	}
	route := func(origin, dest, nh string) error {
		if !known[dest] || !known[nh] || dest == origin || nh == origin {
			return errors.New("advert route is invalid")
		}
		if routes[dest] {
			return errors.New("advert repeats a route")
		}
		routes[dest] = true
		return nil
	}
	if m.Format == FormatJSON {
		var w struct {
			Origin string            `json:"o"`
			Seq    uint64            `json:"s"`
			Links  []json.RawMessage `json:"l"`
			Routes []json.RawMessage `json:"r,omitempty"`
		}
		if err := json.Unmarshal(m.Raw, &w); err != nil {
			return err
		}
		if err := start(w.Origin, w.Seq, len(w.Links), len(w.Routes)); err != nil {
			return err
		}
		for _, l := range w.Links {
			var f []any
			if err := json.Unmarshal(l, &f); err != nil || len(f) != 3 {
				return errors.New("advert link is not [peer, cost, state]")
			}
			peer, _ := f[0].(string)
			cost, ok := f[1].(float64)
			if !ok {
				return errors.New("advert link is invalid")
			}
			if err := link(w.Origin, peer, cost, codeState[fmt.Sprint(f[2])]); err != nil {
				return err
			}
		}
		for _, r := range w.Routes {
			var f []string
			if err := json.Unmarshal(r, &f); err != nil || len(f) < 1 || len(f) > 2 {
				return errors.New("advert route is not [dest] or [dest, nextHop]")
			}
			nh := f[0]
			if len(f) == 2 {
				nh = f[1]
			}
			if err := route(w.Origin, f[0], nh); err != nil {
				return err
			}
		}
		return nil
	}
	data := make([]byte, base64.RawStdEncoding.DecodedLen(len(m.Raw)))
	n, err := base64.RawStdEncoding.Decode(data, m.Raw)
	if err != nil || n == 0 || data[0] != 2 {
		return errors.New("compact advert is malformed")
	}
	data = data[1:n]
	next := func() (uint64, bool) {
		v, k := binary.Uvarint(data)
		if k <= 0 {
			return 0, false
		}
		data = data[k:]
		return v, true
	}
	relay := func() (string, bool) {
		v, ok := next()
		if !ok || v > math.MaxUint32 {
			return "", false
		}
		id, ok := ids[uint32(v)]
		return id, ok
	}
	origin, ok1 := relay()
	seq, ok2 := next()
	nLinks, ok3 := next()
	if !ok1 || !ok2 || !ok3 {
		return errors.New("advert from an unknown relay")
	}
	if err := start(origin, seq, int(min(nLinks, uint64(len(known)+1))), 0); err != nil {
		return err
	}
	for range nLinks {
		peer, ok := relay()
		if !ok || len(data) == 0 {
			return errors.New("advert link is invalid")
		}
		state, ok := byteState[data[0]]
		data = data[1:]
		var cost uint64
		if ok && state == StateUp {
			cost, ok = next()
		}
		if !ok {
			return errors.New("advert link is invalid")
		}
		if err := link(origin, peer, float64(cost)/10, state); err != nil {
			return err
		}
	}
	if len(data) == 0 {
		return errors.New("compact advert is malformed")
	}
	has := data[0]
	data = data[1:]
	switch has {
	case 0:
	case 1:
		count, ok := next()
		if !ok || count > uint64(len(known)) {
			return errors.New("advert lists more relays than there are")
		}
		for range count {
			dest, ok1 := relay()
			hv, ok2 := next()
			nh, found := dest, ok2
			if ok2 && hv != 0 {
				nh, found = ids[uint32(min(hv, math.MaxUint32))]
			}
			if !ok1 || !found {
				return errors.New("advert route is invalid")
			}
			if err := route(origin, dest, nh); err != nil {
				return err
			}
		}
	default:
		return errors.New("compact advert is malformed")
	}
	if len(data) != 0 {
		return errors.New("compact advert is malformed")
	}
	return nil
}

// While some relay reads only 1.2.0's advertisements the controller
// configures no leaves (members routing needs every relay's data plane), and
// a relay's advertisements, either format, are then what 1.2.0 takes. One
// with leaves is not, so that gate must hold.
func TestAdvertsWithoutLeavesAreWhat120Reads(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	ms := func(v float64) time.Duration { return time.Duration(v * float64(time.Millisecond)) }
	nums := map[string]uint32{"A": 1, "B": 2, "C": 3, "M": 10}
	ids := map[uint32]string{1: "A", 2: "B", 3: "C"}
	known := map[string]bool{"A": true, "B": true, "C": true}
	relayLinks := [][2]string{{"A", "B"}, {"B", "C"}}
	advert := func(compact, leaves bool) Message {
		t.Helper()
		c := Config{Self: "A", Relays: []string{"B", "C"}, Links: []string{"B"}, RelayLinks: relayLinks}
		if compact {
			c.Numbers = nums
		}
		if leaves {
			c.Leaves, c.Links, c.MemberLinks = []string{"M"}, []string{"B", "M"}, [][2]string{{"A", "M"}}
		}
		rA := New(signed(c), func() time.Time { return now }, nil)
		rB := New(signed(Config{Self: "B", Relays: []string{"A", "C"}, Links: []string{"A", "C"}, Numbers: c.Numbers, RelayLinks: relayLinks}),
			func() time.Time { return now }, nil)
		rC := New(signed(Config{Self: "C", Relays: []string{"A", "B"}, Links: []string{"B"}, Numbers: c.Numbers, RelayLinks: relayLinks}),
			func() time.Time { return now }, nil)
		for range warmSamples {
			rA.Probe("B", ms(10))
			rA.Probe("M", ms(5))
			rB.Probe("A", ms(10))
			rB.Probe("C", ms(10))
			rC.Probe("B", ms(10))
		}
		// With B's advertisement A has a route to C, so its own carries routes too.
		for _, m := range rC.Advert(Budget{JSON: 1000, Compact: 1000}, []string{"B"}) {
			rB.Receive("C", m, Budget{JSON: 1000, Compact: 1000})
		}
		for _, m := range rB.Advert(Budget{JSON: 1000, Compact: 1000}, []string{"A"}) {
			if _, _, err := rA.Receive("B", m, Budget{JSON: 1000, Compact: 1000}); err != nil {
				t.Fatal(err)
			}
		}
		if _, ok := rA.NextHop("C"); !ok {
			t.Fatal("A has no route to C")
		}
		msgs := rA.Advert(Budget{JSON: 1000, Compact: 1000}, []string{"B"})
		if len(msgs) == 0 {
			t.Fatal("no advertisement")
		}
		return msgs[0]
	}
	for _, compact := range []bool{false, true} {
		m := advert(compact, false)
		if want := map[bool]Format{false: FormatJSON, true: FormatCompact}[compact]; m.Format != want {
			t.Fatalf("format %d", m.Format)
		}
		if err := decode120(m, ids, known); err != nil {
			t.Fatalf("compact %v, no leaves: 1.2.0 refuses it: %v", compact, err)
		}
	}
	if err := decode120(advert(true, true), ids, known); err == nil {
		t.Fatal("1.2.0 takes a compact advertisement with a member section")
	}
}
