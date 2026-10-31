package routing

import (
	"bytes"
	"crypto/ed25519"
	"slices"
	"strings"
	"testing"
)

// An advertisement is taken only as its origin signed it: not one a
// neighbor made in another relay's name, nor one it changed, nor an older
// one again. A relay forwards it as the origin signed it.
func TestAdvertSignatures(t *testing.T) {
	s := newSolo(t, []string{"B", "C", "D"}, []string{"B", "D"}, nil)
	s.up("B", 10)
	s.up("D", 10)
	raw := []byte(`{"o":"C","s":5,"l":[["B",1,"u"]]}`)

	forged := Message{Format: FormatJSON, Raw: raw, Sig: advertInput("n", FormatJSON, raw).Sign(testKey("B"))}
	if _, _, err := s.r.Receive("B", forged, Budget{1000, 1000}); err != ErrSignature {
		t.Fatalf("an advertisement B signed for C: %v", err)
	}
	changed := signedBy("C", FormatJSON, raw)
	changed.Raw = []byte(`{"o":"C","s":5,"l":[["B",900,"u"]]}`)
	if _, _, err := s.r.Receive("B", changed, Budget{1000, 1000}); err != ErrSignature {
		t.Fatalf("an advertisement changed on the way: %v", err)
	}
	asJSON := signedBy("C", FormatCompact, raw)
	asJSON.Format = FormatJSON
	if _, _, err := s.r.Receive("B", asJSON, Budget{1000, 1000}); err != ErrSignature {
		t.Fatalf("a signature for another format: %v", err)
	}
	if s.r.lsdb["C"] != nil || s.r.Verified() != 0 {
		t.Fatal("stored or counted")
	}

	good := signedBy("C", FormatJSON, raw)
	fwd, _, err := s.r.Receive("B", good, Budget{1000, 1000})
	if err != nil || len(fwd) != 1 || fwd[0].Peer != "D" || !bytes.Equal(fwd[0].Raw, raw) || !bytes.Equal(fwd[0].Sig, good.Sig) {
		t.Fatalf("forwarded %+v, %v", fwd, err)
	}
	if s.r.Verified() != 1 {
		t.Fatalf("%d verified, want 1", s.r.Verified())
	}
	newer := signedBy("C", FormatJSON, []byte(`{"o":"C","s":6,"l":[["B",2,"u"]]}`))
	if _, _, err := s.r.Receive("B", newer, Budget{1000, 1000}); err != nil {
		t.Fatal(err)
	}
	if fwd, _, err := s.r.Receive("D", good, Budget{1000, 1000}); err != nil || len(fwd) != 0 || s.r.lsdb["C"].seq != 6 {
		t.Fatalf("the older advertisement again: %v %v", fwd, err)
	}
	// What a new neighbor gets is what C signed.
	s.r.sync["B"] = true
	for _, m := range s.r.Sync(Budget{1000, 1000}) {
		if !advertInput("n", m.Format, m.Raw).Verify(testKey("C").Public().(ed25519.PublicKey), m.Sig) {
			t.Fatalf("synced %+v", m)
		}
	}
}

// Routing takes from advertisements only the links the mesh has.
func TestAdvertOnlyRealLinks(t *testing.T) {
	s := newSolo(t, []string{"B", "C", "D"}, []string{"B"}, nil)
	cfg := signed(Config{Self: "A", Relays: []string{"B", "C", "D"}, Links: []string{"B"}, RelayLinks: [][2]string{{"A", "B"}, {"B", "C"}, {"C", "D"}}})
	s.r.Configure(cfg)
	s.up("B", 10)
	// B claims a link to D it does not have: A would reach D through B.
	if _, err := s.advert("B", "B", map[string]float64{"A": 10, "C": 10, "D": 1}, nil); err != nil {
		t.Fatal(err)
	}
	if _, ok := s.r.lsdb["B"].links["D"]; ok {
		t.Fatal("kept a link the mesh does not have")
	}
	if !slices.ContainsFunc(s.ev, func(e string) bool { return strings.HasPrefix(e, "routing.unknown_link B") }) {
		t.Fatalf("events %v", s.ev)
	}
	if _, ok := s.r.lsdb["B"].links["C"]; !ok {
		t.Fatal("dropped a real link")
	}
}
