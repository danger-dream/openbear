package routing

import (
	"testing"
	"time"
)

// A link that carries less than the network MTU from this end costs
// narrowCost more, in what it advertises too, until it carries it again.
func TestNarrowLinkCost(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	r := New(signed(Config{Self: "A", Relays: []string{"B", "C"}, Links: []string{"B", "C"}}), func() time.Time { return now }, nil)
	for range 3 {
		r.Probe("B", 10*time.Millisecond)
		r.Probe("C", 10*time.Millisecond)
	}
	near(t, "cost", link(t, r, "B").Cost, 10)
	r.SetNarrow(map[string]bool{"B": true})
	l := link(t, r, "B")
	near(t, "cost", l.Cost, 10+narrowCost)
	near(t, "advertised", l.Advertised, 10+narrowCost)
	near(t, "the other link", link(t, r, "C").Advertised, 10)
	r.SetNarrow(nil)
	near(t, "advertised", link(t, r, "B").Advertised, 10)
}
