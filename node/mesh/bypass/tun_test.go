package bypass

import (
	"context"
	"errors"
	"net"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

// fakeTUN stands in for a Windows or macOS machine: the interface findPhysical reports, the
// sockets bindIndex is asked to bind (failing while fail is set), and the clock.
type fakeTUN struct {
	mu    sync.Mutex
	index int
	fail  bool
	binds []bindCall
	at    time.Time
}

type bindCall struct {
	network string
	all     bool
	index   int
}

func useFakeTUN(t *testing.T, index int) *fakeTUN {
	f := &fakeTUN{index: index, at: time.Unix(1_800_000_000, 0)}
	oldNow, oldFind, oldBind, oldState := now, findPhysical, bindIndex, StateFile
	now = func() time.Time { f.mu.Lock(); defer f.mu.Unlock(); return f.at }
	findPhysical = func() int { f.mu.Lock(); defer f.mu.Unlock(); return f.index }
	bindIndex = func(_ uintptr, network string, all bool, index int) error {
		f.mu.Lock()
		defer f.mu.Unlock()
		f.binds = append(f.binds, bindCall{network, all, index})
		if f.fail && index != 0 {
			return errors.New("no way out of that interface")
		}
		return nil
	}
	// No Linux marks: the state file is not there.
	StateFile = filepath.Join(t.TempDir(), "none")
	Forget()
	reset := func() {
		tunState.Lock()
		tunState.at = time.Time{}
		tunState.Unlock()
		proxied.Clear()
	}
	reset()
	t.Cleanup(func() {
		now, findPhysical, bindIndex, StateFile = oldNow, oldFind, oldBind, oldState
		Forget()
		reset()
	})
	return f
}

func (f *fakeTUN) advance(d time.Duration) { f.mu.Lock(); f.at = f.at.Add(d); f.mu.Unlock() }
func (f *fakeTUN) set(index int, fail bool) {
	f.mu.Lock()
	f.index, f.fail = index, fail
	f.mu.Unlock()
}
func (f *fakeTUN) taken() []bindCall {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := f.binds
	f.binds = nil
	return out
}

func meshControl(t *testing.T, network, address string) error {
	c, err := net.ListenPacket("udp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	raw, err := c.(*net.UDPConn).SyscallConn()
	if err != nil {
		t.Fatal(err)
	}
	return MeshControl(network, address, raw)
}

func TestMeshSocketsGoOutOfTheMachinesOwnInterfaceWhileATUNProxyHoldsTheRoutes(t *testing.T) {
	f := useFakeTUN(t, 15)
	// A dial to a relay: its own family; a socket listening on all: every family.
	if err := meshControl(t, "tcp4", "203.0.113.38:52000"); err != nil {
		t.Fatal(err)
	}
	if err := meshControl(t, "udp6", "[::]:51999"); err != nil {
		t.Fatal(err)
	}
	if err := meshControl(t, "udp", ":51999"); err != nil {
		t.Fatal(err)
	}
	// An address the proxy's DNS gave leads only into the proxy: left to it.
	if err := meshControl(t, "tcp4", "198.18.0.73:443"); err != nil {
		t.Fatal(err)
	}
	if got, want := f.taken(), []bindCall{{"tcp4", false, 15}, {"udp6", true, 15}, {"udp", true, 15}}; !equal(got, want) {
		t.Fatalf("bound %v, want %v", got, want)
	}
	// What the routes say is kept a few seconds, then read again: the proxy gone, nothing is bound.
	f.set(0, false)
	meshControl(t, "tcp4", "203.0.113.38:52000")
	if got := f.taken(); len(got) != 1 {
		t.Fatalf("within %v the routes are not read again: %v", TunCheckEvery, got)
	}
	f.advance(TunCheckEvery)
	meshControl(t, "tcp4", "203.0.113.38:52000")
	if got := f.taken(); len(got) != 0 {
		t.Fatalf("no proxy, yet bound: %v", got)
	}
}

func TestARelayThatCannotBeReachedDirectlyIsDialledThroughTheProxyForAWhile(t *testing.T) {
	f := useFakeTUN(t, 15)
	l, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	go func() {
		for {
			c, err := l.Accept()
			if err != nil {
				return
			}
			c.Close()
		}
	}()
	addr, d, ctx := l.Addr().String(), net.Dialer{Timeout: 2 * time.Second}, context.Background()
	dial := func() {
		t.Helper()
		c, err := DialMesh(ctx, d, addr)
		if err != nil {
			t.Fatal(err)
		}
		c.Close()
	}
	// Directly, when that connects.
	dial()
	if got := f.taken(); !equal(got, []bindCall{{"tcp4", false, 15}}) {
		t.Fatalf("direct dial bound %v", got)
	}
	if _, ok := proxied.Load(addr); ok {
		t.Fatal("a direct connection is not one through the proxy")
	}
	// Not directly: through the proxy, and so for proxiedFor without trying directly first.
	f.set(15, true)
	dial()
	f.taken()
	if _, ok := proxied.Load(addr); !ok {
		t.Fatal("the relay reached through the proxy is not kept")
	}
	f.set(15, false)
	f.advance(proxiedFor - time.Second)
	dial()
	if got := f.taken(); len(got) != 0 {
		t.Fatalf("within %v the relay is dialled through the proxy, yet bound %v", proxiedFor, got)
	}
	f.advance(time.Second)
	dial()
	if got := f.taken(); !equal(got, []bindCall{{"tcp4", false, 15}}) {
		t.Fatalf("after %v the relay is tried directly again: %v", proxiedFor, got)
	}
	// Reached neither way: the direct dial's error, and nothing kept.
	proxied.Clear()
	l.Close()
	f.set(15, true)
	if _, err := DialMesh(ctx, d, addr); err == nil {
		t.Fatal("a relay that is down is not reached")
	}
	if _, ok := proxied.Load(addr); ok {
		t.Fatal("a relay down both ways is not one only the proxy reaches")
	}
	f.taken()
	// No proxy: a plain dial, nothing bound.
	f.set(0, false)
	f.advance(TunCheckEvery)
	DialMesh(ctx, d, addr)
	if got := f.taken(); len(got) != 0 {
		t.Fatalf("no proxy, yet bound: %v", got)
	}
}

func TestTheUDPSocketGoesBackThroughTheProxyWhileNothingComesBackDirectly(t *testing.T) {
	f := useFakeTUN(t, 15)
	var set []int
	u := &UDP{set: func(index int) error { set = append(set, index); return nil }}
	check := func(want int) {
		t.Helper()
		u.Check()
		if u.Bound() != want {
			t.Fatalf("bound to %d, want %d (calls %v)", u.Bound(), want, set)
		}
	}
	check(15)
	// Sending with answers coming back: it stays.
	for range 20 {
		f.advance(TunCheckEvery)
		u.Sent()
		u.Received()
		check(15)
	}
	// Sending, nothing back for udpSilent: through the proxy, for proxiedFor.
	for i := 1; i < int(udpSilent/TunCheckEvery); i++ {
		f.advance(TunCheckEvery)
		u.Sent()
		check(15)
	}
	f.advance(TunCheckEvery)
	u.Sent()
	check(0)
	f.advance(proxiedFor - 2*TunCheckEvery)
	u.Sent()
	check(0)
	f.advance(2 * TunCheckEvery)
	check(15)
	// Not sending at all (no peers) is not silence; the proxy gone, it is unbound.
	f.advance(2 * udpSilent)
	check(15)
	f.set(0, false)
	f.advance(TunCheckEvery)
	check(0)
	if want := []int{15, 0, 15, 0}; !equalInts(set, want) {
		t.Fatalf("set %v, want %v", set, want)
	}
	// Linux: nothing to do, and nil does nothing.
	if NewUDP(nil) != nil {
		t.Fatal("Linux keeps its UDP socket past a proxy by marks")
	}
	var none *UDP
	none.Sent()
	none.Received()
	none.Check()
}

func equal(a, b []bindCall) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func equalInts(a, b []int) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
