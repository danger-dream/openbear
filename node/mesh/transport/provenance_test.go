package transport

import (
	"bytes"
	"encoding/binary"
	"fmt"
	"net"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"golang.zx2c4.com/wireguard/conn"
)

type tapPlane struct {
	*PlaneBind
	mu             sync.Mutex
	hold, copyNext bool
	packet         []byte
	meta           conn.SendMetadata
	key            [32]byte
	captured       chan struct{}
	accepted       atomic.Uint64
}

func (p *tapPlane) AuthenticatedReceive(m conn.ReceiveMetadata) bool {
	ok := p.PlaneBind.AuthenticatedReceive(m)
	if ok && m.MessageType == 4 {
		p.accepted.Add(1)
	}
	return ok
}
func (p *tapPlane) SendWithMetadata(bufs [][]byte, key [32]byte, m conn.SendMetadata) error {
	p.mu.Lock()
	drop := false
	if len(bufs) == 1 && len(bufs[0]) > 32 && binary.LittleEndian.Uint32(bufs[0]) == 4 && (p.hold || p.copyNext) {
		p.packet = append([]byte(nil), bufs[0]...)
		p.meta = m
		p.key = key
		drop = p.hold
		p.hold = false
		p.copyNext = false
		if p.captured != nil {
			close(p.captured)
		}
	}
	p.mu.Unlock()
	if drop {
		return nil
	}
	return p.PlaneBind.SendWithMetadata(bufs, key, m)
}
func (p *tapPlane) arm(hold bool) <-chan struct{} {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.hold = hold
	p.copyNext = !hold
	p.captured = make(chan struct{})
	return p.captured
}
func (p *tapPlane) capture() ([]byte, [32]byte, conn.SendMetadata) {
	p.mu.Lock()
	defer p.mu.Unlock()
	return append([]byte(nil), p.packet...), p.key, p.meta
}
func waitControl(t *testing.T, to *controlWG, want []byte) metaPacket {
	t.Helper()
	timer := time.NewTimer(3 * time.Second)
	defer timer.Stop()
	for {
		select {
		case p := <-to.tun.in:
			if bytes.Equal(p.data, want) {
				return p
			}
		case <-timer.C:
			t.Fatal("missing control payload")
		}
	}
}
func queueControl(t *testing.T, l *pathLab, ref PathRef, label string, seq uint64) []byte {
	t.Helper()
	ep, err := l.a.PathEndpoint(ref)
	if err != nil {
		t.Fatal(err)
	}
	want := controlPacket(l.ac, l.zc, label)
	l.ac.tun.out <- metaPacket{data: want, send: conn.SendMetadata{Endpoint: ep, ID: seq}}
	return want
}

func TestAuthenticatedProvenanceReplayAndWrongIdentity(t *testing.T) {
	l := newPathLab(t)
	ref, remote := l.prepare(t, l.tcp)
	l.commit(t, ref, remote)
	captured := l.tap.arm(false)
	want := queueControl(t, l, ref, "once-only", 400)
	first := waitControl(t, l.zc, want)
	<-captured
	info, ok := l.z.PathInfo(first.recv.Endpoint)
	if !ok || info.Ref != remote || first.recv.PublicKey != l.ac.public || !info.Authenticated {
		t.Fatalf("unauthenticated/wrong provenance %+v", info)
	}
	baseline := l.ztap.accepted.Load()
	raw, key, m := l.tap.capture()
	m.ID = 0
	for i := 0; i < 3; i++ {
		if err := l.a.ControlBind().SendWithMetadata([][]byte{raw}, key, m); err != nil {
			t.Fatal(err)
		}
	}
	raw[len(raw)-1] ^= 1
	if err := l.a.ControlBind().SendWithMetadata([][]byte{raw}, key, m); err != nil {
		t.Fatal(err)
	}
	barrier := queueControl(t, l, ref, "barrier", 401)
	timer := time.NewTimer(3 * time.Second)
	defer timer.Stop()
	for {
		select {
		case p := <-l.zc.tun.in:
			if bytes.Equal(p.data, want) {
				t.Fatal("replay reached authenticated control TUN")
			}
			if bytes.Equal(p.data, barrier) {
				goto checked
			}
		case <-timer.C:
			t.Fatal("barrier not delivered")
		}
	}
checked:
	if got := l.ztap.accepted.Load(); got != baseline+1 {
		t.Fatalf("replay/forgery reached authentication callback: %d -> %d", baseline, got)
	}
	wrong := conn.ReceiveMetadata{Endpoint: first.recv.Endpoint, PublicKey: key32(l.ad.public), MessageType: 4}
	if l.z.ControlBind().AuthenticatedReceive(wrong) {
		t.Fatal("data key accepted in control plane")
	}
	var other [32]byte
	other[0] = 1
	var otherCtrl [32]byte
	otherCtrl[0] = 2
	if err := l.z.RegisterPeer(PeerIdentity{ID: "other", DataKey: other, ControlKey: otherCtrl}); err != nil {
		t.Fatal(err)
	}
	wrong.PublicKey = otherCtrl
	if l.z.ControlBind().AuthenticatedReceive(wrong) {
		t.Fatal("different valid peer adopted existing path")
	}
	if err := l.z.RemovePeer("a"); err != nil {
		t.Fatal(err)
	}
	if l.z.ControlBind().AuthenticatedReceive(first.recv) {
		t.Fatal("revoked peer resurrected")
	}
}

func TestControlReplaySpaceSurvivesBusyData(t *testing.T) {
	l := newPathLab(t)
	active, remote := l.prepare(t, l.tcp)
	l.commit(t, active, remote)
	captured := l.tap.arm(true)
	want := queueControl(t, l, active, "delayed-control", 500)
	select {
	case <-captured:
	case <-time.After(time.Second):
		t.Fatal("control not captured")
	}
	for i := 0; i < 8300; i++ {
		transfer(t, l.ad, l.zd, fmt.Sprintf("advance-data-counter-%d", i))
	}
	raw, key, m := l.tap.capture()
	if err := l.a.ControlBind().SendWithMetadata([][]byte{raw}, key, m); err != nil {
		t.Fatal(err)
	}
	received := waitControl(t, l.zc, want)
	info, ok := l.z.PathInfo(received.recv.Endpoint)
	if !ok || info.Ref != remote {
		t.Fatal("lost path across delayed control")
	}
	t.Log("held one control ciphertext while 8300 business packets advanced the business replay space; control still accepted")
}

func TestAuthenticatedOneWayNewPathSurvivesDeadline(t *testing.T) {
	l := newPathLab(t)
	active, remote := l.prepare(t, l.tcp)
	l.commit(t, active, remote)
	c, err := net.Dial("tcp", strings.TrimPrefix(l.tcp, "tcp://"))
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	if err = exchangePreamble(c); err != nil {
		t.Fatal(err)
	}
	captured := l.tap.arm(true)
	want := queueControl(t, l, active, "one-way-path", 600)
	<-captured
	raw, _, _ := l.tap.capture()
	if err = writePlaneFrame(c, raw, maxDatagram(1400), 1); err != nil {
		t.Fatal(err)
	}
	p := waitControl(t, l.zc, want)
	info, ok := l.z.PathInfo(p.recv.Endpoint)
	if !ok || info.Ref == remote {
		t.Fatal("missing actual incoming path")
	}
	l.z.mu.Lock()
	session := l.z.ids[info.Ref.ID].current
	authenticated := session.authenticated
	l.z.mu.Unlock()
	if !authenticated {
		t.Fatal("receive-only authenticated path not confirmed")
	}
	time.Sleep(11 * time.Second)
	captured = l.tap.arm(true)
	want = queueControl(t, l, active, "one-way-still-alive", 601)
	<-captured
	raw, _, _ = l.tap.capture()
	if err = writePlaneFrame(c, raw, maxDatagram(1400), 1); err != nil {
		t.Fatal(err)
	}
	waitControl(t, l.zc, want)
	if session.closed.Load() {
		t.Fatal("legal one-way path closed by old ten-second heuristic")
	}
	l.z.mu.Lock()
	same := l.z.peers["a"].active == remote
	l.z.mu.Unlock()
	if !same {
		t.Fatal("one-way traffic selected standby without commit")
	}
}

func TestControlBindCloseDoesNotCloseBusiness(t *testing.T) {
	l := newPathLab(t)
	active, remote := l.prepare(t, l.tcp)
	l.commit(t, active, remote)
	if err := l.ac.dev.Down(); err != nil {
		t.Fatal(err)
	}
	transfer(t, l.ad, l.zd, "control-down")
	transfer(t, l.zd, l.ad, "business-return")
	if err := l.ac.dev.Up(); err != nil {
		t.Fatal(err)
	}
	sendControl(t, l.ac, l.zc, l.a, active, "control-reopened", 800)
}
