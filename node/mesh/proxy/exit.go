package proxy

import (
	"context"
	"errors"
	"io"
	"net"
	"net/netip"
	"os"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

// headerWait is how long an exit waits for the header of a tunnel the
// entry opened ahead of a client; entries use theirs well before.
const headerWait = 60 * time.Second

const dialTimeout = 10 * time.Second

const minDialShare = 2 * time.Second

// udpIdle ends a UDP association nothing went through for this long.
const udpIdle = 3 * time.Minute

type exitStats struct {
	Active, Opened, Refused, Failed, BytesIn, BytesOut atomic.Int64
}

// exitServer accepts tunnels on this node's mesh address.
type exitServer struct {
	ln      net.Listener
	clients map[netip.Addr]bool
	policy  *policy
	iface   string
	source  netip.Addr
	stats   *exitStats
	wg      sync.WaitGroup
	conns   *conns
	// allow, when set, replaces the policy: tests dial their own loopback.
	allow func(netip.Addr) bool
}

// startExit takes tunnels for x on addr; open ones are counted in open, which
// the exit that takes over from it goes on with.
func startExit(addr netip.AddrPort, x *Exit, mesh []netip.Prefix, stats *exitStats, self *localAddrs, open *conns) (*exitServer, error) {
	ln, err := net.Listen("tcp", addr.String())
	if err != nil {
		return nil, err
	}
	s := &exitServer{ln: ln, clients: map[netip.Addr]bool{}, policy: &policy{lan: x.LAN, mesh: mesh, self: self},
		iface: x.Interface, source: x.Source, stats: stats, conns: open}
	for _, c := range x.Clients {
		s.clients[c] = true
	}
	s.wg.Add(1)
	go s.accept()
	return s, nil
}

// close stops taking tunnels; those open run on.
func (s *exitServer) close() {
	s.ln.Close()
	s.wg.Wait()
}

func (s *exitServer) accept() {
	defer s.wg.Done()
	for {
		c, err := s.ln.Accept()
		if err != nil {
			if errors.Is(err, net.ErrClosed) {
				return
			}
			time.Sleep(50 * time.Millisecond)
			continue
		}
		tc := c.(*net.TCPConn)
		from, _ := netip.ParseAddrPort(c.RemoteAddr().String())
		if !s.clients[from.Addr().Unmap()] || !s.conns.add(tc, from.Addr().Unmap()) {
			s.stats.Refused.Add(1)
			c.Close()
			continue
		}
		go s.serve(tc)
	}
}

func (s *exitServer) allowed(a netip.Addr) bool {
	if s.allow != nil {
		return s.allow(a)
	}
	return s.policy.allowed(a)
}

func (s *exitServer) serve(c *net.TCPConn) {
	defer s.conns.done(c)
	defer c.Close()
	c.SetReadDeadline(time.Now().Add(headerWait))
	cmd, target, err := readHeader(c)
	if err != nil {
		return
	}
	c.SetReadDeadline(time.Time{})
	s.stats.Active.Add(1)
	defer s.stats.Active.Add(-1)
	s.stats.Opened.Add(1)
	switch cmd {
	case cmdConnect:
		out, status := s.dialTCP(target)
		if _, err := c.Write([]byte{status}); err != nil || status != statusOK {
			if out != nil {
				out.Close()
			}
			return
		}
		defer out.Close()
		if to, err := netip.ParseAddrPort(out.RemoteAddr().String()); err == nil {
			s.conns.reached(c, to.Addr().Unmap())
		}
		in, outBytes := relay(c, out)
		s.stats.BytesIn.Add(in)
		s.stats.BytesOut.Add(outBytes)
	case cmdUDP:
		s.udp(c)
	default:
		c.Write([]byte{statusCommand})
	}
}

// resolve gives the addresses of a that the exit may dial, IPv4 first and
// then alternating families, or the status to answer.
func (s *exitServer) resolve(ctx context.Context, a Addr) ([]netip.Addr, byte) {
	ips := []netip.Addr{a.IP}
	if a.Host != "" {
		found, err := net.DefaultResolver.LookupNetIP(ctx, "ip", a.Host)
		if err != nil || len(found) == 0 {
			return nil, statusHost
		}
		ips = found
	}
	var ok []netip.Addr
	for _, ip := range ips {
		if ip = ip.Unmap(); s.allowed(ip) {
			ok = append(ok, ip)
		}
	}
	if len(ok) == 0 {
		s.stats.Refused.Add(1)
		return nil, statusNotAllowed
	}
	return interleave(ok), statusOK
}

func interleave(ips []netip.Addr) []netip.Addr {
	var v4, v6 []netip.Addr
	for _, ip := range ips {
		if ip.Is4() {
			v4 = append(v4, ip)
		} else {
			v6 = append(v6, ip)
		}
	}
	out := make([]netip.Addr, 0, len(ips))
	for i := 0; i < len(v4) || i < len(v6); i++ {
		if i < len(v4) {
			out = append(out, v4[i])
		}
		if i < len(v6) {
			out = append(out, v6[i])
		}
	}
	return out
}

func (s *exitServer) dialer() *net.Dialer {
	d := &net.Dialer{Timeout: dialTimeout, Control: bindControl(s.iface)}
	if s.source.IsValid() {
		d.LocalAddr = &net.TCPAddr{IP: s.source.AsSlice()}
	}
	return d
}

func (s *exitServer) dialTCP(a Addr) (*net.TCPConn, byte) {
	ctx, cancel := context.WithTimeout(context.Background(), dialTimeout)
	defer cancel()
	ips, status := s.resolve(ctx, a)
	if status != statusOK {
		return nil, status
	}
	d := s.dialer()
	for i, ip := range ips {
		// One silent address must not take the whole budget from the rest.
		actx, acancel := ctx, context.CancelFunc(func() {})
		if deadline, ok := ctx.Deadline(); ok && i < len(ips)-1 {
			share := time.Until(deadline) / time.Duration(len(ips)-i)
			actx, acancel = context.WithTimeout(ctx, max(share, minDialShare))
		}
		c, err := d.DialContext(actx, "tcp", netip.AddrPortFrom(ip, a.Port).String())
		acancel()
		if err == nil {
			return c.(*net.TCPConn), statusOK
		}
		status = dialStatus(err)
		if ctx.Err() != nil {
			break
		}
	}
	s.stats.Failed.Add(1)
	return nil, status
}

func dialStatus(err error) byte {
	var ne net.Error
	switch {
	case errors.Is(err, syscall.ECONNREFUSED):
		return statusRefused
	case errors.Is(err, syscall.ENETUNREACH):
		return statusNetwork
	case errors.Is(err, syscall.EHOSTUNREACH):
		return statusHost
	case errors.Is(err, os.ErrDeadlineExceeded), errors.Is(err, context.DeadlineExceeded), errors.As(err, &ne) && ne.Timeout():
		return statusTimeout
	}
	return statusFailure
}

// relay copies both ways until both are done, half-closing each side as
// its source ends; between two TCP connections Linux splices. It returns
// the bytes from a to b and from b to a.
func relay(a, b *net.TCPConn) (int64, int64) {
	done := make(chan int64, 1)
	go func() {
		n, _ := io.Copy(b, a)
		b.CloseWrite()
		done <- n
	}()
	n, _ := io.Copy(a, b)
	a.CloseWrite()
	return <-done, n
}

// udp relays a UDP association: frames from the tunnel go out from one
// socket, and what comes back to it goes back as frames.
func (s *exitServer) udp(c *net.TCPConn) {
	lc := net.ListenConfig{Control: bindControl(s.iface)}
	local := ":0"
	if s.source.IsValid() {
		local = netip.AddrPortFrom(s.source, 0).String()
	}
	raw, err := lc.ListenPacket(context.Background(), "udp", local)
	if err != nil {
		c.Write([]byte{statusFailure})
		return
	}
	pc := raw.(*net.UDPConn)
	defer pc.Close()
	if _, err := c.Write([]byte{statusOK}); err != nil {
		return
	}
	var last atomic.Int64
	last.Store(time.Now().UnixNano())
	resolved := map[string]netip.Addr{}
	go func() {
		defer pc.Close()
		buf := make([]byte, 0x10000)
		for {
			a, payload, err := readFrame(c, buf)
			if err != nil {
				return
			}
			ip := a.IP
			if a.Host != "" {
				if ip = resolved[a.Host]; !ip.IsValid() {
					ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
					ips, status := s.resolve(ctx, a)
					cancel()
					if status != statusOK {
						continue
					}
					ip = ips[0]
					if len(resolved) > 256 {
						clear(resolved)
					}
					resolved[a.Host] = ip
				}
			} else if !s.allowed(ip.Unmap()) {
				s.stats.Refused.Add(1)
				continue
			}
			last.Store(time.Now().UnixNano())
			if n, err := pc.WriteToUDPAddrPort(payload, netip.AddrPortFrom(ip, a.Port)); err == nil {
				s.stats.BytesOut.Add(int64(n))
			}
		}
	}()
	buf := make([]byte, 0x10000)
	for {
		pc.SetReadDeadline(time.Now().Add(30 * time.Second))
		n, from, err := pc.ReadFromUDPAddrPort(buf)
		if err != nil {
			var ne net.Error
			if errors.As(err, &ne) && ne.Timeout() && time.Since(time.Unix(0, last.Load())) < udpIdle {
				continue
			}
			c.Close()
			return
		}
		last.Store(time.Now().UnixNano())
		s.stats.BytesIn.Add(int64(n))
		if writeFrame(c, Addr{IP: from.Addr().Unmap(), Port: from.Port()}, buf[:n]) != nil {
			return
		}
	}
}
