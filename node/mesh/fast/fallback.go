package fast

import (
	"context"
	"encoding/binary"
	"errors"
	"io"
	"net"
	"net/netip"
	"sync"

	"gvisor.dev/gvisor/pkg/buffer"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/adapters/gonet"
	iphdr "gvisor.dev/gvisor/pkg/tcpip/header"
	linkchan "gvisor.dev/gvisor/pkg/tcpip/link/channel"
	"gvisor.dev/gvisor/pkg/tcpip/network/ipv4"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"
	"gvisor.dev/gvisor/pkg/tcpip/transport/udp"
)

// sourceFallback is only used after capture, for non-SNAT LAN sources whose
// fast path cannot open. Its packets use the ordinary mesh route and ACLs,
// keeping the original source instead of silently becoming the gateway.
// This is a shared, lazy stack; fast streams keep their existing splice path.
type sourceFallback struct {
	stack  *stack.Stack
	ep     *linkchan.Endpoint
	cancel context.CancelFunc
	ctx    context.Context
	dials  sync.WaitGroup
	done   chan struct{}
	mu     sync.Mutex
	closed bool
	flows  map[fallbackKey]bool
}
type fallbackKey struct {
	proto         byte
	local, remote netip.AddrPort
}

func newSourceFallback(send func([]byte), mtu uint32) (*sourceFallback, error) {
	if send == nil {
		return nil, errors.New("no mesh data plane for source-preserving fallback")
	}
	if mtu < 576 {
		mtu = 1280
	}
	st := stack.New(stack.Options{NetworkProtocols: []stack.NetworkProtocolFactory{ipv4.NewProtocol}, TransportProtocols: []stack.TransportProtocolFactory{tcp.NewProtocol, udp.NewProtocol}})
	ep := linkchan.New(256, mtu, "")
	if err := st.CreateNIC(1, ep); err != nil {
		st.Close()
		ep.Close()
		return nil, errors.New(err.String())
	}
	_ = st.SetPromiscuousMode(1, true)
	_ = st.SetSpoofing(1, true)
	st.AddRoute(tcpip.Route{Destination: iphdr.IPv4EmptySubnet, NIC: 1})
	ctx, cancel := context.WithCancel(context.Background())
	f := &sourceFallback{stack: st, ep: ep, cancel: cancel, ctx: ctx, done: make(chan struct{}), flows: map[fallbackKey]bool{}}
	go func() {
		defer close(f.done)
		for {
			pkt := ep.ReadContext(ctx)
			if pkt == nil {
				return
			}
			v := pkt.ToView()
			pkt.DecRef()
			// The hook takes ownership of its bytes for asynchronous mesh scheduling.
			send(append([]byte(nil), v.AsSlice()...))
			v.Release()
		}
	}()
	return f, nil
}

func (f *sourceFallback) close() {
	f.mu.Lock()
	if f.closed {
		f.mu.Unlock()
		return
	}
	f.closed = true
	clear(f.flows)
	f.mu.Unlock()
	f.cancel()
	f.dials.Wait() // no endpoint may be created after Stack.Close's snapshot
	f.stack.Close()
	f.ep.Close()
	<-f.done
	f.stack.Wait()
}

func (f *sourceFallback) take(b []byte) bool {
	if len(b) < 20 || b[0]>>4 != 4 {
		return false
	}
	n := int(b[0]&15) * 4
	if n < 20 || len(b) < n {
		return false
	}
	// ICMP errors quote our original outgoing tuple (not the reply tuple).
	if b[9] == 1 && len(b) >= n+8+24 && (b[n] == 3 || b[n] == 11 || b[n] == 12) {
		q := b[n+8:]
		qh := int(q[0]&15) * 4
		if qh < 20 || len(q) < qh+4 {
			return false
		}
		proto := byte(protoTCP)
		if q[9] == 17 {
			proto = protoUDP
		} else if q[9] != 6 {
			return false
		}
		k := fallbackKey{proto, netip.AddrPortFrom(netip.AddrFrom4([4]byte(q[12:16])), binary.BigEndian.Uint16(q[qh:])), netip.AddrPortFrom(netip.AddrFrom4([4]byte(q[16:20])), binary.BigEndian.Uint16(q[qh+2:]))}
		f.mu.Lock()
		defer f.mu.Unlock()
		if f.closed || !f.flows[k] {
			return false
		}
		f.inject(b)
		return true
	}
	proto := byte(0)
	switch b[9] {
	case 6:
		proto = protoTCP
	case 17:
		proto = protoUDP
	default:
		return false
	}
	// Later fragments lack ports. Give a copy to the reassembler only for
	// an active address pair, but do not consume it: the first fragment may
	// still belong to a different LAN flow, which must keep all its packets.
	if binary.BigEndian.Uint16(b[6:8])&0x1fff != 0 {
		f.mu.Lock()
		defer f.mu.Unlock()
		if f.closed {
			return false
		}
		for k := range f.flows {
			if k.proto == proto && k.local.Addr() == netip.AddrFrom4([4]byte(b[16:20])) && k.remote.Addr() == netip.AddrFrom4([4]byte(b[12:16])) {
				f.inject(b)
				break
			}
		}
		return false
	}
	if len(b) < n+4 {
		return false
	}
	k := fallbackKey{proto, netip.AddrPortFrom(netip.AddrFrom4([4]byte(b[16:20])), binary.BigEndian.Uint16(b[n+2:])), netip.AddrPortFrom(netip.AddrFrom4([4]byte(b[12:16])), binary.BigEndian.Uint16(b[n:]))}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.closed || !f.flows[k] {
		return false
	}
	f.inject(b)
	return true
}

func (f *sourceFallback) inject(b []byte) {
	pkt := stack.NewPacketBuffer(stack.PacketBufferOptions{Payload: buffer.MakeWithData(b)})
	f.ep.InjectInbound(iphdr.IPv4ProtocolNumber, pkt)
	pkt.DecRef()
}

func (f *sourceFallback) dial(ctx context.Context, proto byte, local, remote netip.AddrPort) (net.Conn, error) {
	k := fallbackKey{proto, local, remote}
	f.mu.Lock()
	if f.closed || f.flows[k] {
		f.mu.Unlock()
		return nil, net.ErrClosed
	}
	f.flows[k] = true
	f.dials.Add(1)
	f.mu.Unlock()
	defer f.dials.Done()
	ctx, cancel := context.WithCancel(ctx)
	stop := context.AfterFunc(f.ctx, cancel)
	defer func() { stop(); cancel() }()
	forget := func() { f.mu.Lock(); delete(f.flows, k); f.mu.Unlock() }
	la := tcpip.FullAddress{NIC: 1, Addr: tcpip.AddrFrom4(local.Addr().As4()), Port: local.Port()}
	ra := tcpip.FullAddress{NIC: 1, Addr: tcpip.AddrFrom4(remote.Addr().As4()), Port: remote.Port()}
	var c net.Conn
	var err error
	if proto == protoTCP {
		c, err = gonet.DialTCPWithBind(ctx, f.stack, la, ra, ipv4.ProtocolNumber)
	} else {
		c, err = gonet.DialUDP(f.stack, &la, &ra, ipv4.ProtocolNumber)
	}
	if err != nil {
		forget()
		return nil, err
	}
	if err = ctx.Err(); err != nil {
		c.Close()
		forget()
		return nil, err
	}
	return &fallbackConn{Conn: c, forget: forget}, nil
}

type fallbackConn struct {
	net.Conn
	once   sync.Once
	forget func()
}

func (c *fallbackConn) Close() error { err := c.Conn.Close(); c.once.Do(c.forget); return err }
func (c *fallbackConn) CloseWrite() error {
	return c.Conn.(interface{ CloseWrite() error }).CloseWrite()
}

func (s *Service) sourceDial(ctx context.Context, proto byte, local, remote netip.AddrPort) (net.Conn, error) {
	s.mu.Lock()
	if s.state.Load() == nil {
		s.mu.Unlock()
		return nil, net.ErrClosed
	}
	f := s.fallback.Load()
	if f == nil {
		var err error
		mtu := uint32(1280)
		if s.hooks.MeshMTU != nil {
			mtu = s.hooks.MeshMTU()
		}
		f, err = newSourceFallback(s.hooks.ToMesh, mtu)
		if err != nil {
			s.mu.Unlock()
			return nil, err
		}
		s.fallback.Store(f)
	}
	s.mu.Unlock()
	return f.dial(ctx, proto, local, remote)
}

// Inbound consumes only a fallback flow's replies, after mesh source and ACL
// checks. All other packets, including ordinary LAN connections, keep going.
func (s *Service) Inbound(b []byte) bool { f := s.fallback.Load(); return f != nil && f.take(b) }

func relaySource(a *net.TCPConn, b net.Conn) (int64, int64) {
	ch := make(chan int64, 1)
	go func() {
		n, err := io.Copy(b, a)
		if err != nil {
			a.Close()
			b.Close()
		} else {
			_ = b.(interface{ CloseWrite() error }).CloseWrite()
		}
		ch <- n
	}()
	n, err := io.Copy(a, b)
	if err != nil {
		a.Close()
		b.Close()
	} else {
		a.CloseWrite()
	}
	return <-ch, n
}
