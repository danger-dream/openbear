// Package bypass keeps the node's own measurements and the mesh data plane
// off a transparent proxy on the same machine (OpenClash, PassWall, sing-box,
// mihomo...), whatever proxy it is: the sockets they open carry Mark and are
// created under the group GID. When the node finds such a proxy it puts in
// place an ip rule that sends Mark to the main table, an nftables chain that
// sets Mark again on GID's packets after any proxy re-marked them, and one
// that holds GID's TCP to its destination before a proxy redirects it. Until
// then (or with no proxy) none of this is set and sockets are left alone.
package bypass

import (
	"context"
	"net"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

const (
	// Mark routes by the main table; high bits, clear of the low marks proxies use.
	Mark = 0x4f420000
	// GID owns the bypassed sockets, which nftables tells apart by it.
	GID = 52999
	// RulePref is ahead of every proxy's routing rules.
	RulePref = 100
	// Table is the nftables table holding the chains.
	Table = "openbear_bypass"
)

// StateFile exists while the node has the rules in place; it is in RAM, gone
// with the rules at the next boot. Tests point it elsewhere.
var StateFile = "/var/run/openbear-bypass"

var cache struct {
	sync.Mutex
	at     time.Time
	active bool
}

// Active reports whether the rules are in place, as of the last two seconds.
func Active() bool {
	cache.Lock()
	defer cache.Unlock()
	if now := time.Now(); now.Sub(cache.at) > 2*time.Second {
		_, err := os.Stat(StateFile)
		cache.at, cache.active = now, err == nil
	}
	return cache.active
}

// Forget drops what Active last read, for the node right after it changed the rules.
func Forget() {
	cache.Lock()
	cache.at = time.Time{}
	cache.Unlock()
}

// Control marks a socket before it binds or connects, so its route is chosen
// with Mark: for net.Dialer and net.ListenConfig.
func Control(_, _ string, c syscall.RawConn) error {
	if !Active() {
		return nil
	}
	return control(c)
}

// Socket runs fn, which opens sockets, so they are created under GID: fn has
// this thread to itself and opens them on it (one address to dial, no
// goroutines of its own).
func Socket(fn func() error) error {
	if !Active() {
		return fn()
	}
	return socket(fn)
}

// Dialer is net.Dialer bypassing the proxy while the rules are in place.
func Dialer(timeout time.Duration) *net.Dialer {
	return &net.Dialer{Timeout: timeout, Control: Control}
}

// DialContext dials as d does, under GID while the rules are in place.
func DialContext(ctx context.Context, d *net.Dialer, network, address string) (c net.Conn, err error) {
	err = Socket(func() error {
		c, err = d.DialContext(ctx, network, address)
		return err
	})
	return c, err
}

// resolvConfs list the system's upstream DNS servers, ahead of
// /etc/resolv.conf, which on such machines points at the proxy's own DNS.
var resolvConfs = []string{"/tmp/resolv.conf.d/resolv.conf.auto", "/tmp/resolv.conf.auto", "/run/systemd/resolve/resolv.conf", "/etc/resolv.conf"}

// fallbackDNS answer when no upstream is found.
var fallbackDNS = []string{"223.5.5.5", "1.1.1.1"}

// Resolver resolves names for a measurement. With the rules in place it asks
// the upstream DNS servers itself over bypassed sockets, not the system's
// resolver: a proxy's DNS answers with fake addresses (198.18.0.0/15) that only
// lead into the proxy.
func Resolver() *net.Resolver {
	if !Active() {
		return net.DefaultResolver
	}
	servers := upstreams()
	// A UDP "connection" opens whether or not the server answers: each dial, the resolver's
	// retry after a timeout among them, starts at the next server.
	var turn atomic.Uint32
	return &net.Resolver{PreferGo: true, Dial: func(ctx context.Context, network, _ string) (net.Conn, error) {
		first := int(turn.Add(1) - 1)
		var last error
		for i := range servers {
			s := servers[(first+i)%len(servers)]
			c, err := DialContext(ctx, Dialer(2*time.Second), network, net.JoinHostPort(s, "53"))
			if err == nil {
				if u, ok := c.(*net.UDPConn); ok {
					return upstreamUDP{u}, nil
				}
				return c, nil
			}
			last = err
		}
		return nil, last
	}}
}

// upstreamWait bounds each wait for an upstream's answer, shorter than the resolver's own
// (resolv.conf's, 5 s by default), so one that never answers is soon left for the next.
const upstreamWait = 2 * time.Second

// upstreamUDP is a UDP socket to an upstream whose deadlines go no further than upstreamWait.
type upstreamUDP struct{ *net.UDPConn }

func (u upstreamUDP) SetDeadline(t time.Time) error { return u.UDPConn.SetDeadline(soon(t)) }

func (u upstreamUDP) SetReadDeadline(t time.Time) error { return u.UDPConn.SetReadDeadline(soon(t)) }

func soon(t time.Time) time.Time {
	if limit := time.Now().Add(upstreamWait); t.IsZero() || t.After(limit) {
		return limit
	}
	return t
}

// Upstreams are the system's upstream DNS servers (or the fallbacks), as Resolver asks them.
func Upstreams() []string { return upstreams() }

func upstreams() []string {
	for _, path := range resolvConfs {
		raw, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		var out []string
		for _, line := range strings.Split(string(raw), "\n") {
			f := strings.Fields(line)
			if len(f) < 2 || f[0] != "nameserver" {
				continue
			}
			if ip := net.ParseIP(f[1]); ip != nil && !ip.IsLoopback() && !ip.IsUnspecified() && !fakeIP(ip) {
				out = append(out, f[1])
			}
		}
		if len(out) > 0 {
			return out
		}
	}
	return fallbackDNS
}

var fakeNet = &net.IPNet{IP: net.IPv4(198, 18, 0, 0), Mask: net.CIDRMask(15, 32)}

func fakeIP(ip net.IP) bool { return fakeNet.Contains(ip) }
