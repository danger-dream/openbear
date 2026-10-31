package transport

import (
	"bytes"
	"strconv"
	"strings"
	"testing"
	"time"

	"golang.zx2c4.com/wireguard/device"
)

func handshakeSeconds(t *testing.T, d *device.Device) int64 {
	t.Helper()
	state, err := d.IpcGet()
	if err != nil {
		t.Fatal(err)
	}
	for _, line := range strings.Split(state, "\n") {
		if s, ok := strings.CutPrefix(line, "last_handshake_time_sec="); ok {
			v, _ := strconv.ParseInt(s, 10, 64)
			return v
		}
	}
	return 0
}

func prewarmData(t *testing.T, l *pathLab, local PathRef) {
	t.Helper()
	ep, err := l.a.PathEndpoint(local)
	if err != nil {
		t.Fatal(err)
	}
	peer := l.ad.dev.LookupPeer(device.NoisePublicKey(key32(l.zd.public)))
	if peer == nil {
		t.Fatal("missing data peer")
	}
	deadline := time.Now().Add(8 * time.Second)
	for handshakeSeconds(t, l.zd.dev) == 0 {
		if err := peer.SendHandshakeInitiationTo(false, ep); err != nil {
			t.Fatal(err)
		}
		if time.Now().After(deadline) {
			t.Fatal("passive keypair never confirmed before commit")
		}
		time.Sleep(50 * time.Millisecond)
	}
}

func TestUncommittedDataHandshakeConfirmsOnRequestedPath(t *testing.T) {
	l := newPathLab(t)
	local, remote := l.prepare(t, l.tcp)
	prewarmData(t, l, local)
	l.a.mu.Lock()
	active := l.a.peers["z"].active
	l.a.mu.Unlock()
	l.z.mu.Lock()
	passiveActive := l.z.peers["a"].active
	l.z.mu.Unlock()
	if active.ID != "" || passiveActive.ID != "" {
		t.Fatal("handshake confirmation committed business path")
	}
	select {
	case <-l.ad.tun.Inbound:
		t.Fatal("confirmation delivered application data")
	default:
	}
	select {
	case <-l.zd.tun.Inbound:
		t.Fatal("confirmation delivered application data")
	default:
	}
	if err := l.a.Commit("z", local); err != nil {
		t.Fatal(err)
	}
	if err := l.z.Commit("a", remote); err != nil {
		t.Fatal(err)
	}
	want := dataPacket(l.zd, l.ad, []byte("first traffic toward idle initiator"))
	l.zd.tun.Outbound <- want
	select {
	case got := <-l.ad.tun.Inbound:
		if !bytes.Equal(got, want) {
			t.Fatal("incorrect first reverse packet")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("idle member did not receive first reverse packet")
	}
}

func TestKeepaliveToProbesDataPathWithoutSwitching(t *testing.T) {
	l := newPathLab(t)
	active, remote := l.prepare(t, l.tcp)
	prewarmData(t, l, active)
	if err := l.a.Commit("z", active); err != nil {
		t.Fatal(err)
	}
	if err := l.z.Commit("a", remote); err != nil {
		t.Fatal(err)
	}
	transfer(t, l.ad, l.zd, "confirmed-session-forward")
	transfer(t, l.zd, l.ad, "confirmed-session-return")
	standby, remoteStandby := l.prepare(t, l.tls)
	ep, err := l.a.PathEndpoint(standby)
	if err != nil {
		t.Fatal(err)
	}
	before := handshakeSeconds(t, l.zd.dev)
	peer := l.ad.dev.LookupPeer(device.NoisePublicKey(key32(l.zd.public)))
	peer.SendKeepaliveTo(ep)
	deadline := time.Now().Add(2 * time.Second)
	for {
		l.z.mu.Lock()
		authenticated := l.z.ids[remoteStandby.ID].auth[0]
		l.z.mu.Unlock()
		if authenticated {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("standby not authenticated: local=%+v remote=%+v localStats=%+v", l.a.Paths(), l.z.Paths(), l.a.Stats())
		}
		time.Sleep(10 * time.Millisecond)
	}
	l.a.mu.Lock()
	selected := l.a.peers["z"].active
	l.a.mu.Unlock()
	if selected != active || handshakeSeconds(t, l.zd.dev) != before {
		t.Fatal("explicit keepalive changed active path or reset handshake")
	}
	select {
	case <-l.zd.tun.Inbound:
		t.Fatal("keepalive became business data")
	default:
	}
	transfer(t, l.ad, l.zd, "business remains on active")
}
