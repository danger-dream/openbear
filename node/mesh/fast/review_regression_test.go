package fast

import (
	"context"
	"errors"
	"io"
	"net"
	"net/netip"
	"strings"
	"testing"
	"time"
)

func reviewState(t *testing.T) (*Service, *runtimeState) {
	t.Helper()
	a, b := netip.MustParseAddr("100.96.0.1"), netip.MustParseAddr("100.96.0.2")
	cfg := &Config{Keys: []string{randKeyB64(t)}, Nodes: map[string]netip.Addr{"A": a, "B": b}, Targets: []Target{{Node: "B", Port: 443, Proto: "tcp"}}}
	sealer, err := newSealer(cfg.Keys)
	if err != nil {
		t.Fatal(err)
	}
	targets, dests := targetsOf(cfg, "A")
	st := &runtimeState{cfg: cfg, selfAddr: a, selfID: "A", targets: targets, dests: dests, inbound: []Rule{{Proto: "any"}}, sealer: sealer}
	s := &Service{}
	s.state.Store(st)
	return s, st
}

func TestReviewOldGenerationPoolAfterDisable(t *testing.T) {
	s, st := reviewState(t)
	p := newPool()
	p.configure(st.cfg, "A")
	st.pool, s.pool = p, p
	// Hold an in-flight dial until its lifetime is cancelled, not a real socket.
	started := make(chan struct{}, 1)
	p.dial = func(ctx context.Context, _ string) (*net.TCPConn, error) {
		select {
		case started <- struct{}{}:
		default:
		}
		<-ctx.Done()
		return nil, ctx.Err()
	}
	got := make(chan error, 1)
	go func() {
		_, err := s.sendHeader(st, Plan{Hops: []string{"B"}, Planned: true}, header{proto: protoTCP, source: st.selfAddr, dest: st.cfg.Nodes["B"], port: 443})
		got <- err
	}()
	<-started
	s.mu.Lock()
	s.stopAllLocked()
	s.mu.Unlock()
	select {
	case err := <-got:
		if err == nil {
			t.Fatal("dial survived disable")
		}
	case <-time.After(time.Second):
		t.Fatal("pool dial not cancelled")
	}
	if _, err := p.get("B"); !errors.Is(err, net.ErrClosed) {
		t.Fatalf("closed pool: %v", err)
	}
	if _, err := s.sendHeader(st, Plan{Hops: []string{"B"}}, header{proto: protoTCP, source: st.selfAddr, dest: st.cfg.Nodes["B"], port: 443}); err == nil {
		t.Fatal("old worker opened after stop")
	}
}

func TestReviewAdmissionUsesCurrentPolicy(t *testing.T) {
	s, old := reviewState(t)
	src, dst := old.selfAddr, old.cfg.Nodes["B"]
	stale := trackedConn{src: src, dst: dst, proto: protoTCP, port: 443, role: roleEntry}
	if !old.allows(stale) {
		t.Fatal("fixture")
	}
	next := *old
	next.targets = map[targetKey]*Target{}
	// Deterministically resume admission after policy publication/revocation.
	s.openMu.Lock()
	s.state.Store(&next)
	s.openMu.Unlock()
	s.closeAllConns(func(c trackedConn) bool { return !next.allows(c) })
	c := io.NopCloser(strings.NewReader(""))
	if s.track(c, stale) {
		t.Fatal("stale policy admitted after revoke")
	}
	next.inbound = nil
	exit := trackedConn{src: src, dst: next.selfAddr, proto: protoUDP, port: 53, role: roleExit}
	if s.track(c, exit) {
		t.Fatal("late UDP exit admitted")
	}
	s.state.Store(old)
	if !s.track(c, stale) {
		t.Fatal("still-authorized entry refused")
	}
	s.closeAllConns(func(c trackedConn) bool { return !old.allows(c) })
	if !s.streamsTo("") {
		t.Fatal("unrelated policy update cut an allowed stream")
	}
}

func TestReviewPendingHeaderLimitIsSeparate(t *testing.T) {
	s, st := reviewState(t)
	var first *net.TCPConn
	for i := 0; i < pendingHeaderLimit(); i++ {
		c := new(net.TCPConn)
		if first == nil {
			first = c
		}
		if !s.admitHeader(c) {
			t.Fatal("premature limit")
		}
	}
	if s.admitHeader(new(net.TCPConn)) {
		t.Fatal("unbounded pre-auth sockets")
	}
	c := io.NopCloser(strings.NewReader(""))
	if !s.track(c, trackedConn{role: roleExit, src: st.selfAddr, dst: st.selfAddr, proto: protoTCP, port: 443}) {
		t.Fatal("pending limit consumed business capacity")
	}
	s.headerDone(first)
	if !s.admitHeader(new(net.TCPConn)) {
		t.Fatal("header slot not returned")
	}
	s.mu.Lock()
	s.stopAllLocked()
	s.mu.Unlock()
	if len(s.pending) != 0 || s.admitHeader(new(net.TCPConn)) {
		t.Fatal("pending state survived disable")
	}
}

func TestReviewActiveChannelDoesNotExpire(t *testing.T) {
	s, st := reviewState(t)
	now := time.Now()
	s.now = func() time.Time { return now }
	c := &channels{svc: s, byDest: map[string]*channel{"B": {dest: "B", lastUse: now.Add(-2 * channelIdle), refreshed: now}}, wake: make(chan struct{}, 1)}
	st.chans = c
	s.chans = c
	stream := io.NopCloser(strings.NewReader(""))
	if !s.trackStream(stream, opened{src: st.selfAddr, dst: st.cfg.Nodes["B"], dest: "B"}, protoTCP, 443) {
		t.Fatal("track")
	}
	c.tick()
	if c.byDest["B"] == nil {
		t.Fatal("active destination expired")
	}
	s.untrackConn(stream)
	c.tick()
	if c.byDest["B"] != nil {
		t.Fatal("unused destination never expired")
	}
}

func TestReviewDialCancellationAfterLoopbackConnect(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	s := &Service{dialLn: ln}
	entered := make(chan struct{})
	release := make(chan struct{})
	go func() {
		c, e := ln.Accept()
		if e != nil {
			return
		}
		defer c.Close()
		io.ReadFull(c, make([]byte, dialPreamble))
		close(entered)
		<-release
	}()
	defer close(release)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() {
		c, e := s.Dial(ctx, netip.MustParseAddrPort("100.96.0.2:443"))
		if c != nil {
			c.Close()
		}
		done <- e
	}()
	<-entered
	cancel()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("cancel: %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("Dial ignored cancellation")
	}
}
