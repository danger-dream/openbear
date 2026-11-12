package bypass

import (
	"context"
	"net"
	"net/netip"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

// A TUN-mode proxy on a Windows or macOS machine (Clash/mihomo, sing-box, Surge...) takes the routes
// to the internet, and with them the mesh's connections to its relays, which then go the proxy's way
// round. While one does, the mesh data plane's sockets go out of the machine's own interface, bound
// to it as such proxies bind their own. A relay that cannot be reached so is dialled through the
// proxy after all, for a while; the UDP socket goes back through it while nothing comes back. On
// Linux the marks above do this, and none of this is used.

const (
	// TunCheckEvery: how long what the routes say is kept, and how often the UDP socket looks again.
	TunCheckEvery = 5 * time.Second
	// proxiedFor: how long a relay (or the UDP socket) that could not be reached directly is left
	// to the proxy before being tried directly again.
	proxiedFor = 10 * time.Minute
	// udpSilent: sending with nothing coming back for this long, the UDP socket goes back through the proxy.
	udpSilent = time.Minute
)

var (
	// now and findPhysical are replaced by tests; findPhysical is the platform's (tun_*.go).
	now          = time.Now
	findPhysical = physicalInterface
	// bindIndex binds a socket to an interface (0: unbinds it); all: for every address family
	// (a socket with no address of its own yet, as one listening on all).
	bindIndex = bindInterface
)

// fakeIPs is what a proxy's DNS answers with; such an address leads only into the proxy.
var fakeIPs = netip.MustParsePrefix("198.18.0.0/15")

var tunState struct {
	sync.Mutex
	at    time.Time
	index int
}

// physical is the interface the mesh's sockets are bound to now: the machine's own, while a TUN
// proxy holds the routes to the internet; 0 when none does.
func physical() int {
	tunState.Lock()
	defer tunState.Unlock()
	if t := now(); tunState.at.IsZero() || t.Sub(tunState.at) >= TunCheckEvery {
		tunState.at, tunState.index = t, findPhysical()
	}
	return tunState.index
}

// MeshControl is Control for the mesh data plane's sockets: past a TUN proxy too.
func MeshControl(network, address string, c syscall.RawConn) error {
	if err := Control(network, address, c); err != nil {
		return err
	}
	return bindPhysical(network, address, c, physical())
}

func bindPhysical(network, address string, c syscall.RawConn, index int) error {
	if index == 0 {
		return nil
	}
	all := true
	if ap, err := netip.ParseAddrPort(address); err == nil {
		if fakeIPs.Contains(ap.Addr().Unmap()) {
			return nil
		}
		all = ap.Addr().IsUnspecified()
	}
	var serr error
	if err := c.Control(func(fd uintptr) { serr = bindIndex(fd, network, all, index) }); err != nil {
		return err
	}
	return serr
}

// proxied holds the relays (address -> when) last reached only through the proxy.
var proxied sync.Map

// DialMesh dials a relay over TCP for the mesh, with d's timeouts: past a TUN proxy while one holds
// the routes, and through it when that does not connect.
func DialMesh(ctx context.Context, d net.Dialer, address string) (net.Conn, error) {
	d.Control = MeshControl
	if physical() == 0 {
		return d.DialContext(ctx, "tcp", address)
	}
	if at, ok := proxied.Load(address); ok && now().Sub(at.(time.Time)) < proxiedFor {
		d.Control = Control
		return d.DialContext(ctx, "tcp", address)
	}
	c, err := d.DialContext(ctx, "tcp", address)
	if err == nil || ctx.Err() != nil {
		return c, err
	}
	d.Control = Control
	c, perr := d.DialContext(ctx, "tcp", address)
	if perr != nil {
		return nil, err
	}
	proxied.Store(address, now())
	return c, nil
}

// UDP keeps the mesh's UDP socket past a TUN proxy: bound to the machine's own interface while one
// holds the routes; back through the proxy when it has sent for udpSilent with nothing coming back,
// and past it again proxiedFor later. Nil where there is nothing to do (Linux); its methods take nil.
type UDP struct {
	sent, received atomic.Int64 // unix nanoseconds
	set            func(index int) error

	mu    sync.Mutex
	index int       // bound to now; 0: not bound
	since time.Time // when it was bound
	back  time.Time // through the proxy until then
}

// NewUDP keeps the UDP socket c past a TUN proxy, as Check does: call it right away and then every
// TunCheckEvery. Nil on Linux.
func NewUDP(c syscall.RawConn) *UDP {
	if !tunSupported {
		return nil
	}
	return &UDP{set: func(index int) error {
		var serr error
		if err := c.Control(func(fd uintptr) { serr = bindIndex(fd, "udp", true, index) }); err != nil {
			return err
		}
		return serr
	}}
}

// Sent and Received note a datagram going out and one coming in.
func (u *UDP) Sent() {
	if u != nil {
		u.sent.Store(now().UnixNano())
	}
}

func (u *UDP) Received() {
	if u != nil {
		u.received.Store(now().UnixNano())
	}
}

// Check binds or unbinds the socket as the routes and what came back say.
func (u *UDP) Check() {
	if u == nil {
		return
	}
	u.mu.Lock()
	defer u.mu.Unlock()
	t, want := now(), physical()
	if u.index != 0 && want == u.index && t.Sub(u.since) >= udpSilent {
		sent, got := time.Unix(0, u.sent.Load()), time.Unix(0, u.received.Load())
		if t.Sub(sent) < 2*TunCheckEvery && t.Sub(got) >= udpSilent {
			u.back = t.Add(proxiedFor)
		}
	}
	if t.Before(u.back) {
		want = 0
	}
	if want != u.index && u.set(want) == nil {
		u.index, u.since = want, t
	}
}

// Bound is the interface the socket is bound to; 0 when it is not.
func (u *UDP) Bound() int {
	if u == nil {
		return 0
	}
	u.mu.Lock()
	defer u.mu.Unlock()
	return u.index
}
