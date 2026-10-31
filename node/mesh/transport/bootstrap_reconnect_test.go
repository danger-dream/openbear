package transport

import (
	"testing"
	"time"
)

// From the 10-30 check: after a backup TCP path reconnected, the control plane kept sending
// through its old generation (use of closed network connection) until the data plane restarted.
func TestTheBootstrapPathFollowsItsReconnect(t *testing.T) {
	l := newPathLab(t)
	ref, _ := l.prepare(t, l.tcp)
	l.z.mu.Lock()
	for ss := range l.z.run.sessions {
		ss.conn.Close()
	}
	l.z.mu.Unlock()
	waitUntil(t, 10*time.Second, "redialed", func() bool {
		l.a.mu.Lock()
		defer l.a.mu.Unlock()
		ep := l.a.ids[ref.ID]
		return ep != nil && ep.generation > ref.Generation && ep.current != nil && ep.current.ready
	})
	logical, err := l.a.ParseEndpoint("mesh-peer://z")
	if err != nil {
		t.Fatal(err)
	}
	l.a.mu.Lock()
	_, err = l.a.resolve(logical, 1, false)
	boot := l.a.peers["z"].bootstrap
	l.a.mu.Unlock()
	if err != nil {
		t.Fatalf("the control plane resolves through %v: %v", boot, err)
	}
}
