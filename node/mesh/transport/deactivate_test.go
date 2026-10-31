package transport

import (
	"errors"
	"net"
	"testing"
)

func TestDeactivateKeepsPeerAndStandby(t *testing.T) {
	l := newPathLab(t)
	active, remote := l.prepare(t, l.tcp)
	l.commit(t, active, remote)
	standby, _ := l.prepare(t, l.tls)
	l.a.mu.Lock()
	original := l.a.ids[active.ID].current
	l.a.mu.Unlock()
	if err := l.a.Deactivate("z", active); err != nil {
		t.Fatal(err)
	}
	if original.closed.Load() {
		t.Fatal("deactivate closed physical connection")
	}
	logical, err := l.a.ParseEndpoint("mesh-peer://z")
	if err != nil {
		t.Fatal("identity removed", err)
	}
	if err := l.a.Send([][]byte{packet4()}, logical); err != ErrNoPath || !errors.Is(err, net.ErrClosed) {
		t.Fatalf("ordinary business still selected uncommitted path: %v", err)
	}
	l.a.mu.Lock()
	bootstrap := l.a.peers["z"].bootstrap
	l.a.mu.Unlock()
	if bootstrap != (PathRef{}) {
		t.Fatal("bootstrap not cleared")
	}
	sendControl(t, l.ac, l.zc, l.a, active, "deactivated-but-probeable", 1001)
	if err := l.a.ReleasePath(active); err != nil {
		t.Fatal(err)
	}
	if _, err := l.a.PathEndpoint(standby); err != nil {
		t.Fatal("standby released", err)
	}
	if _, err := l.a.PeerEndpoint("z"); err != nil {
		t.Fatal("control identity removed", err)
	}
}

func TestDeactivateCASAndDisconnectedGeneration(t *testing.T) {
	b := newBind(t, Options{})
	var data, control [32]byte
	data[0], control[0] = 1, 2
	if err := b.RegisterPeer(PeerIdentity{ID: "A", DataKey: data, ControlKey: control}); err != nil {
		t.Fatal(err)
	}
	data[0], control[0] = 3, 4
	if err := b.RegisterPeer(PeerIdentity{ID: "B", DataKey: data, ControlKey: control}); err != nil {
		t.Fatal(err)
	}
	old, current, other := PathRef{"p1", 1}, PathRef{"p1", 2}, PathRef{"p2", 1}
	b.mu.Lock()
	b.peers["A"].active, b.peers["A"].bootstrap = current, current
	b.peers["B"].active, b.peers["B"].bootstrap = other, other
	b.mu.Unlock()
	for _, ref := range []PathRef{old, other, {}} {
		if err := b.Deactivate("A", ref); !errors.Is(err, ErrPathChanged) {
			t.Fatalf("CAS accepted %v: %v", ref, err)
		}
	}
	if err := b.Deactivate("A", current); err != nil {
		t.Fatal("cannot retire selection after its connection disappeared", err)
	}
	b.mu.Lock()
	a, z := *b.peers["A"], *b.peers["B"]
	b.mu.Unlock()
	if a.active != (PathRef{}) || a.bootstrap != (PathRef{}) || z.active != other || z.bootstrap != other {
		t.Fatal("deactivate changed another peer or left old selection")
	}
	event := <-b.Events()
	if event.Kind != "deactivated" || event.Path.Ref != current || event.Path.PeerID != "A" || event.Path.Active {
		t.Fatalf("incorrect disconnected path event: %+v", event)
	}
	if err := b.Deactivate("A", current); !errors.Is(err, ErrPathChanged) {
		t.Fatalf("repeated CAS should report stale expectation: %v", err)
	}
	if err := b.Deactivate("missing", current); !errors.Is(err, net.ErrClosed) {
		t.Fatal(err)
	}
}
