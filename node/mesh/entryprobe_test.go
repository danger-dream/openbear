package mesh

import (
	"testing"
	"time"
)

// A probe through the entry reaches the other relay's data plane, past its
// filter (it has no rules) and not into its OS, and the reply clears the
// member's wait; a reply with another id does not.
func TestEntryProbeIsAnsweredByTheDataPlane(t *testing.T) {
	l := newLab(t, spec{relays: []string{"R1", "R2"}, members: map[string][]string{"A": {"R1"}}})
	n := l.up()
	a, r1, r2 := n["A"], n["R1"], n["R2"]
	ep := a.tun.entryProbe
	probe := func(id [8]byte, want bool) {
		t.Helper()
		ep.mu.Lock()
		ep.entry, ep.target, ep.waiting, ep.id = "R1", "R2", true, [8]byte{1, 2, 3}
		ep.mu.Unlock()
		z := a.tun.route.Load().nodes.byID["R2"]
		a.e2e.sendControl(probePacket(l.addr("A"), l.addr("R2"), probeRequest, id, 777), z)
		deadline := time.Now().Add(5 * time.Second)
		for time.Now().Before(deadline) {
			ep.mu.Lock()
			waiting := ep.waiting
			ep.mu.Unlock()
			if !waiting {
				if !want {
					t.Fatal("a reply with another id cleared the wait")
				}
				return
			}
			time.Sleep(20 * time.Millisecond)
		}
		if want {
			t.Fatal("no reply to the probe")
		}
	}
	probe([8]byte{1, 2, 3}, true)
	if r1.tun.stats.Forwarded.Load() < 2 {
		t.Fatalf("R1 forwarded %d: the probe did not go through the entry", r1.tun.stats.Forwarded.Load())
	}
	expectNone(t, r2, "the probe in R2's OS")
	if r2.tun.stats.DroppedACL.Load() != 0 || a.e2e.status().Sent != 0 {
		t.Fatalf("ACL drops %d, counted as traffic %d", r2.tun.stats.DroppedACL.Load(), a.e2e.status().Sent)
	}
	probe([8]byte{9}, false)
}
