package transport

import (
	"bytes"
	"testing"
	"time"
)

// Preparing control must leave data cold, not start failed pre-commit
// handshakes whose retransmit timers can collide after commit. Exercise the
// first data packet without prewarmData, retries, or waiting for RekeyTimeout.
func TestPathLabColdDataAfterControlCommit(t *testing.T) {
	l := newPathLab(t)
	local, remote := l.prepare(t, l.tcp)
	for _, n := range []*testWG{l.ad, l.zd} {
		if handshakeSeconds(t, n.dev) != 0 {
			t.Fatal("control prepare unexpectedly completed a data handshake")
		}
	}
	for _, b := range []*Bind{l.a, l.z} {
		for _, p := range b.Paths() {
			if p.DataAuthenticated || !p.ControlAuthenticated {
				t.Fatalf("expected control-only prepared path: %+v", p)
			}
		}
	}
	if err := l.a.Commit("z", local); err != nil {
		t.Fatal(err)
	}
	if err := l.z.Commit("a", remote); err != nil {
		t.Fatal(err)
	}
	for _, direction := range []struct{ from, to *testWG }{{l.ad, l.zd}, {l.zd, l.ad}} {
		want := dataPacket(direction.from, direction.to, []byte("single cold-start packet"))
		timer := time.NewTimer(2 * time.Second)
		select {
		case direction.from.tun.Outbound <- want:
		case <-timer.C:
			t.Fatal("cold-start TUN send blocked")
		}
		select {
		case got := <-direction.to.tun.Inbound:
			timer.Stop()
			if !bytes.Equal(got, want) {
				t.Fatal("incorrect cold-start packet")
			}
		case <-timer.C:
			t.Fatalf("cold-start packet waited for a pre-commit handshake retry: from=%s to=%s", wgPacketState(direction.from), wgPacketState(direction.to))
		}
	}
}
