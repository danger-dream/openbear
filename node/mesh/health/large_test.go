package health

import (
	"testing"
	"time"

	"openbear/node/mesh/wire"
)

type largeProtoBackend struct {
	*protoBackend
	large map[string]bool
}

func (b *largeProtoBackend) SetPeerLarge(peer string, large bool) { b.large[peer] = large }

// A node that takes large frames says so in its control messages; a peer
// is taken to take them only when it says so in a range that has
// wire.LargeFrames, until it says otherwise. Protocol 2 and 3 nodes never
// say it, and what they would is not believed.
func TestLargeFramesDeclaredAndLearned(t *testing.T) {
	for _, declare := range []bool{true, false} {
		m, pb := versionManager(t, wire.Local())
		m.cfg.LargeFrames = declare
		b := &largeProtoBackend{protoBackend: pb, large: map[string]bool{}}
		m.backend = b
		for _, c := range []struct {
			peer      string
			v, lo, hi int
			lf        bool
			want      bool
		}{
			{"new", wire.LargeFrames, 2, wire.LargeFrames, true, true},
			{"old", 3, 2, 3, true, false},
			{"older", 2, 2, 2, false, false},
			{"newer", wire.LargeFrames, 2, wire.LargeFrames, false, false},
		} {
			var extra map[string]any
			if c.lf {
				extra = map[string]any{"lf": true}
			}
			m.receive(ping(t, c.peer, c.v, c.lo, c.hi, extra))
			if got, told := b.large[c.peer]; got != c.want || !told {
				t.Fatalf("%s: told large %v (%v), want %v", c.peer, got, told, c.want)
			}
			sent, ok := lastSent(pb)
			if !ok || sent.LargeFrames != declare || sent.Max != wire.Protocol {
				t.Fatalf("%s: answer declares lf %v range %d..%d", c.peer, sent.LargeFrames, sent.Min, sent.Max)
			}
		}
		// A peer that stops saying it no longer gets large frames.
		m.receive(ping(t, "new", wire.LargeFrames, 2, wire.LargeFrames, nil))
		if b.large["new"] {
			t.Fatal("peer still taken to take large frames")
		}
		// Control held up for longer than the range lasts leaves them be:
		// the peer's streams still take them.
		m.receive(ping(t, "new", wire.LargeFrames, 2, wire.LargeFrames, map[string]any{"lf": true}))
		m.expireProtocols(time.Now().Add(protocolExpiry))
		if !b.large["new"] {
			t.Fatal("large frames withdrawn as control went quiet")
		}
		// Each message says it again, for streams opened since.
		delete(b.large, "new")
		m.receive(ping(t, "new", wire.LargeFrames, 2, wire.LargeFrames, map[string]any{"lf": true}))
		m.receive(ping(t, "new", wire.LargeFrames, 2, wire.LargeFrames, map[string]any{"lf": true}))
		if !b.large["new"] {
			t.Fatal("not told again")
		}
	}
}
