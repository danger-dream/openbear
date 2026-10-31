package transport

import (
	"context"
	"net"
	"net/netip"
	"strconv"
	"sync"
	"testing"
	"time"
)

// fakeNames stands in for DNS: what a name resolves to can change.
type fakeNames struct {
	mu      sync.Mutex
	addrs   []netip.Addr
	lookups int
}

func (f *fakeNames) set(addrs ...string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.addrs = nil
	for _, a := range addrs {
		f.addrs = append(f.addrs, netip.MustParseAddr(a))
	}
}

func (f *fakeNames) lookup(_ context.Context, _, _ string) ([]netip.Addr, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.lookups++
	return append([]netip.Addr(nil), f.addrs...), nil
}

func (f *fakeNames) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.lookups
}

func swapNames(t *testing.T, f *fakeNames, route func(netip.Addr) bool, refresh time.Duration) {
	t.Helper()
	oldLookup, oldRoute, oldRefresh := lookupNetIP, routable, nameRefresh
	lookupNetIP, routable, nameRefresh = f.lookup, route, refresh
	t.Cleanup(func() { lookupNetIP, routable, nameRefresh = oldLookup, oldRoute, oldRefresh })
}

// A relay named by DDNS that moves to a new address: the path took the
// name's address once and kept sending to the old one until the service
// restarted. It now follows the name, and stays put while the name still
// lists the address in use, as round-robin DNS does.
func TestNamedEndpointFollowsItsAddress(t *testing.T) {
	sinks := map[string]chan struct{}{}
	port := 0
	for _, ip := range []string{"127.0.0.1", "127.0.0.2"} {
		c, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.ParseIP(ip), Port: port})
		if err != nil {
			t.Skipf("no %s: %v", ip, err)
		}
		defer c.Close()
		port = c.LocalAddr().(*net.UDPAddr).Port
		got := make(chan struct{}, 64)
		sinks[ip] = got
		go func() {
			buf := make([]byte, 2048)
			for {
				if _, _, err := c.ReadFromUDP(buf); err != nil {
					return
				}
				select {
				case got <- struct{}{}:
				default:
				}
			}
		}()
	}
	names := &fakeNames{}
	names.set("127.0.0.1")
	swapNames(t, names, func(netip.Addr) bool { return true }, 50*time.Millisecond)
	b := newBind(t, Options{})
	openBind(t, b)
	uri := "udp://relay.example:" + strconv.Itoa(port)
	ep, err := b.ParseEndpoint(uri)
	if err != nil {
		t.Fatal(err)
	}
	reaches := func(ip string) bool {
		t.Helper()
		for _, ch := range sinks {
			for len(ch) > 0 {
				<-ch
			}
		}
		deadline := time.Now().Add(3 * time.Second)
		for time.Now().Before(deadline) {
			if err := b.Send([][]byte{make([]byte, 100)}, ep); err != nil {
				t.Fatal(err)
			}
			select {
			case <-sinks[ip]:
				return true
			case <-time.After(50 * time.Millisecond):
			}
		}
		return false
	}
	if !reaches("127.0.0.1") {
		t.Fatal("the name's first address gets nothing")
	}
	looked := names.count()
	if again, err := b.ParseEndpoint(uri); err != nil || again != ep || names.count() > looked+1 {
		t.Fatalf("an existing path is looked up again on every parse: %v", err)
	}
	names.set("127.0.0.2")
	if !reaches("127.0.0.2") {
		t.Fatal("the path stayed on the old address")
	}
	if s := b.Stats(); len(s) != 1 || s[0].RemoteAddr != "127.0.0.2:"+strconv.Itoa(port) {
		t.Fatalf("stats: %+v", s)
	}
	names.set("127.0.0.3", "127.0.0.2")
	for before := names.count(); names.count() < before+3; {
		time.Sleep(20 * time.Millisecond)
	}
	if !reaches("127.0.0.2") {
		t.Fatal("a name that still lists the address in use moved the path")
	}
}

// IPv4 first, unless this machine has no way to it: on an IPv6-only host a
// dual-stack name gave the IPv4 address and the path never worked.
func TestResolveRemotePicksAReachableFamily(t *testing.T) {
	names := &fakeNames{}
	names.set("2001:db8::7", "192.0.2.7")
	swapNames(t, names, func(netip.Addr) bool { return true }, time.Minute)
	resolve := func() string {
		got, err := resolveRemote(context.Background(), "relay.example:51820", netip.AddrPort{})
		if err != nil {
			t.Fatal(err)
		}
		return got.String()
	}
	if got := resolve(); got != "192.0.2.7:51820" {
		t.Fatalf("dual stack: %v", got)
	}
	routable = func(ip netip.Addr) bool { return ip.Is6() }
	if got := resolve(); got != "[2001:db8::7]:51820" {
		t.Fatalf("IPv6 only: %v", got)
	}
	routable = func(netip.Addr) bool { return false }
	if got := resolve(); got != "192.0.2.7:51820" {
		t.Fatalf("no route at all keeps the usual choice: %v", got)
	}
	got, err := resolveRemote(context.Background(), "[::ffff:198.51.100.1]:9", netip.AddrPort{})
	if err != nil || got.String() != "198.51.100.1:9" || names.count() != 3 {
		t.Fatalf("a literal is not looked up and loses its mapping: %v %v", got, err)
	}
}
