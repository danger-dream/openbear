//go:build !linux || fastnetstack

package fast

import (
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"net/netip"
	"sync"
	"sync/atomic"
	"time"

	"gvisor.dev/gvisor/pkg/buffer"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/adapters/gonet"
	"gvisor.dev/gvisor/pkg/tcpip/checksum"
	iphdr "gvisor.dev/gvisor/pkg/tcpip/header"
	linkchan "gvisor.dev/gvisor/pkg/tcpip/link/channel"
	"gvisor.dev/gvisor/pkg/tcpip/network/ipv4"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"
	"gvisor.dev/gvisor/pkg/waiter"
)

// tunCapture takes, in the data plane, this machine's connections to the fast
// ports whose channel is ready: a user-space TCP/IP stack (gVisor's) answers
// TCP in the destination's place, and UDP datagrams are carried as they come.
// A connection keeps the way it started: one begun over the mesh stays there.
type tunCapture struct {
	svc      *Service
	toOS     func([]byte)
	sessions *udpSessionTable
	scope    uint64
	stack    *stack.Stack
	ep       *linkchan.Endpoint
	cancel   context.CancelFunc
	ipID     atomic.Uint32

	mu     sync.Mutex
	flows  map[flowKey]*capFlow
	closed bool
	wg     sync.WaitGroup
}

type flowKey struct {
	proto    byte
	src, dst netip.AddrPort
}

// capFlow is a connection taken: last is when its last packet came, live
// that its TCP stream is carried now.
type capFlow struct {
	last atomic.Int64
	live atomic.Bool
}

const (
	// maxCapFlows bounds the connections taken at once; more go over the mesh.
	maxCapFlows = 8192
	// tcpFlowLinger keeps a TCP flow that ended for its last segments, which
	// the stack answers rather than the destination over the mesh.
	tcpFlowLinger  = 30 * time.Second
	flowSweepEvery = 10 * time.Second
	stackNIC       = 1
)

var flowSweep = flowSweepEvery

func newTunCapture(s *Service, toOS func([]byte), sessions *udpSessionTable) (*tunCapture, error) {
	if toOS == nil || sessions == nil {
		return nil, errors.New("no data plane to capture in")
	}
	st := stack.New(stack.Options{
		NetworkProtocols:   []stack.NetworkProtocolFactory{ipv4.NewProtocol},
		TransportProtocols: []stack.TransportProtocolFactory{tcp.NewProtocol},
	})
	sack := tcpip.TCPSACKEnabled(true)
	moderate := tcpip.TCPModerateReceiveBufferOption(true)
	rcv := tcpip.TCPReceiveBufferSizeRangeOption{Min: 4 << 10, Default: 1 << 20, Max: 8 << 20}
	snd := tcpip.TCPSendBufferSizeRangeOption{Min: 4 << 10, Default: 1 << 20, Max: 8 << 20}
	for _, opt := range []tcpip.SettableTransportProtocolOption{&sack, &moderate, &rcv, &snd} {
		if err := st.SetTransportProtocolOption(tcp.ProtocolNumber, opt); err != nil {
			st.Close()
			return nil, fmt.Errorf("tcp option: %v", err)
		}
	}
	// The stack's MSS follows the client's: the machine's own interface MTU.
	ep := linkchan.New(4096, 65535, "")
	ep.LinkEPCapabilities |= stack.CapabilityRXChecksumOffload
	if err := st.CreateNIC(stackNIC, ep); err != nil {
		st.Close()
		return nil, fmt.Errorf("stack nic: %v", err)
	}
	_ = st.SetPromiscuousMode(stackNIC, true)
	_ = st.SetSpoofing(stackNIC, true)
	st.AddRoute(tcpip.Route{Destination: iphdr.IPv4EmptySubnet, NIC: stackNIC})

	ctx, cancel := context.WithCancel(context.Background())
	tc := &tunCapture{svc: s, toOS: toOS, sessions: sessions, scope: sessions.newScope(), stack: st, ep: ep, cancel: cancel, flows: map[flowKey]*capFlow{}}
	fwd := tcp.NewForwarder(st, 0, 2048, tc.handleTCP)
	st.SetTransportProtocolHandler(tcp.ProtocolNumber, fwd.HandlePacket)
	tc.wg.Add(2)
	go tc.writeOut(ctx)
	go tc.sweepLoop(ctx)
	return tc, nil
}

func (tc *tunCapture) close() {
	if tc == nil {
		return
	}
	tc.mu.Lock()
	if tc.closed {
		tc.mu.Unlock()
		return
	}
	tc.closed = true
	tc.flows = map[flowKey]*capFlow{}
	tc.mu.Unlock()
	tc.cancel()
	tc.sessions.dropScope(tc.scope)
	tc.stack.Close()
	tc.ep.Close()
	tc.wg.Wait()
	tc.stack.Wait()
}

func (tc *tunCapture) status() *CaptureStatus {
	return &CaptureStatus{Mode: "tun", Active: true, UDP: true}
}

// take is Outbound's for a packet to dest's fast port: one of a connection
// taken already, or one that opens a connection (a SYN, a datagram) while
// dest's channel is ready.
func (tc *tunCapture) take(b []byte, ihl int, proto byte, opens bool, dest string) bool {
	k := flowKey{proto: proto,
		src: netip.AddrPortFrom(netip.AddrFrom4([4]byte(b[12:16])), binary.BigEndian.Uint16(b[ihl:])),
		dst: netip.AddrPortFrom(netip.AddrFrom4([4]byte(b[16:20])), binary.BigEndian.Uint16(b[ihl+2:]))}
	tc.mu.Lock()
	f := tc.flows[k]
	if f == nil {
		if tc.closed || !opens || !tc.svc.isReady(dest) || len(tc.flows) >= maxCapFlows {
			tc.mu.Unlock()
			return false
		}
		f = &capFlow{}
		tc.flows[k] = f
	}
	tc.mu.Unlock()
	f.last.Store(tc.svc.clock().UnixNano())
	if proto == protoUDP {
		tc.udp(b, ihl, k)
		return true
	}
	pkb := stack.NewPacketBuffer(stack.PacketBufferOptions{Payload: buffer.MakeWithData(b)})
	tc.ep.InjectInbound(iphdr.IPv4ProtocolNumber, pkb)
	pkb.DecRef()
	return true
}

func (tc *tunCapture) flow(k flowKey) *capFlow {
	tc.mu.Lock()
	defer tc.mu.Unlock()
	return tc.flows[k]
}

func (tc *tunCapture) forget(k flowKey) {
	tc.mu.Lock()
	delete(tc.flows, k)
	tc.mu.Unlock()
}

// writeOut gives the machine what the stack sends: its answers to the
// connections it took.
func (tc *tunCapture) writeOut(ctx context.Context) {
	defer tc.wg.Done()
	for {
		pkt := tc.ep.ReadContext(ctx)
		if pkt == nil {
			return
		}
		v := pkt.ToView()
		pkt.DecRef()
		tc.toOS(v.AsSlice())
		v.Release()
	}
}

func (tc *tunCapture) sweepLoop(ctx context.Context) {
	defer tc.wg.Done()
	tick := time.NewTicker(flowSweep)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		}
		now := tc.svc.clock().UnixNano()
		tc.mu.Lock()
		for k, f := range tc.flows {
			idle := tcpFlowLinger
			if k.proto == protoUDP {
				idle = udpIdleFor(k.dst.Port())
			}
			if !f.live.Load() && now-f.last.Load() > int64(idle) {
				delete(tc.flows, k)
			}
		}
		tc.mu.Unlock()
	}
}

// handleTCP carries a SYN the stack took. The stream along the channel opens
// before the handshake completes: with no path taking it the SYN goes
// unanswered and its flow is forgotten, so the client's next SYN goes over
// the mesh.
func (tc *tunCapture) handleTCP(r *tcp.ForwarderRequest) {
	id := r.ID()
	client := netip.AddrPortFrom(netip.AddrFrom4(id.RemoteAddress.As4()), id.RemotePort)
	target := netip.AddrPortFrom(netip.AddrFrom4(id.LocalAddress.As4()), id.LocalPort)
	k := flowKey{proto: protoTCP, src: client, dst: target}
	if f := tc.flow(k); f != nil {
		f.live.Store(true)
		defer func() {
			f.last.Store(tc.svc.clock().UnixNano())
			f.live.Store(false)
		}()
	}
	s := tc.svc
	s.entryRole.opened.Add(1)
	o, err := s.openStream(protoTCP, client, true, target)
	if err != nil {
		if errors.Is(err, errNotAllowed) {
			r.Complete(true)
			return
		}
		tc.forget(k)
		r.Complete(false)
		return
	}
	up := o.up
	defer up.Close()
	var wq waiter.Queue
	ep, terr := r.CreateEndpoint(&wq)
	if terr != nil {
		s.entryRole.failed.Add(1)
		r.Complete(true)
		abortTCP(up)
		return
	}
	r.Complete(false)
	ep.SocketOptions().SetKeepAlive(true)
	c := gonet.NewTCPConn(&wq, ep)
	defer c.Close()
	es := &entryStream{client: c, up: up}
	if !s.trackStream(es, o, protoTCP, target.Port()) {
		s.entryRole.failed.Add(1)
		ep.Abort()
		abortTCP(up)
		return
	}
	defer s.untrackConn(es)
	s.entryRole.active.Add(1)
	defer s.entryRole.active.Add(-1)
	toUp, toClient, aborted := relayStack(c, ep, up)
	s.entryRole.out.Add(toUp)
	s.entryRole.in.Add(toClient)
	if chans := s.live.Load(); aborted && chans != nil {
		chans.recheck(o.dest, o.plan)
	}
}

// relayStack is relayTCP for a connection the stack took: an end closing
// half-closes the other, an error resets both.
func relayStack(c *gonet.TCPConn, ep tcpip.Endpoint, up *net.TCPConn) (toUp, toClient int64, aborted bool) {
	type res struct {
		n   int64
		err error
	}
	ch := make(chan res, 1)
	go func() {
		n, err := io.Copy(up, c)
		if err != nil {
			abortTCP(up)
			ep.Abort()
		} else {
			_ = up.CloseWrite()
		}
		ch <- res{n, err}
	}()
	n, err := io.Copy(c, up)
	if err != nil {
		ep.Abort()
		abortTCP(up)
	} else {
		_ = c.CloseWrite()
	}
	r := <-ch
	return r.n, n, err != nil || r.err != nil
}

// udp carries a datagram of flow k on its session; the answers come back to
// the machine as datagrams from the fast port.
func (tc *tunCapture) udp(b []byte, ihl int, k flowKey) {
	n := int(binary.BigEndian.Uint16(b[ihl+4:]))
	if n < 8 || ihl+n > len(b) {
		return
	}
	payload := append([]byte(nil), b[ihl+8:ihl+n]...)
	key := udpSessionKey{scope: tc.scope, client: k.src, target: k.dst, preserveLAN: true}
	_ = tc.sessions.send(key, payload, func() (func([]byte) int, func(), error) {
		return func(p []byte) int {
			tc.toOS(tc.udpPacket(k.dst, k.src, p))
			return len(p)
		}, nil, nil
	}, nil, nil, nil)
}

// udpPacket is an IPv4 datagram from src to dst carrying p.
func (tc *tunCapture) udpPacket(src, dst netip.AddrPort, p []byte) []byte {
	b := make([]byte, iphdr.IPv4MinimumSize+iphdr.UDPMinimumSize+len(p))
	ip := iphdr.IPv4(b)
	ip.Encode(&iphdr.IPv4Fields{
		TotalLength: uint16(len(b)),
		ID:          uint16(tc.ipID.Add(1)),
		TTL:         64,
		Protocol:    uint8(iphdr.UDPProtocolNumber),
		SrcAddr:     tcpip.AddrFrom4(src.Addr().As4()),
		DstAddr:     tcpip.AddrFrom4(dst.Addr().As4()),
	})
	ip.SetChecksum(^ip.CalculateChecksum())
	u := iphdr.UDP(b[iphdr.IPv4MinimumSize:])
	u.Encode(&iphdr.UDPFields{SrcPort: src.Port(), DstPort: dst.Port(), Length: uint16(iphdr.UDPMinimumSize + len(p))})
	copy(b[iphdr.IPv4MinimumSize+iphdr.UDPMinimumSize:], p)
	sum := iphdr.PseudoHeaderChecksum(iphdr.UDPProtocolNumber, ip.SourceAddress(), ip.DestinationAddress(), uint16(iphdr.UDPMinimumSize+len(p)))
	sum = checksum.Checksum(p, sum)
	if c := ^u.CalculateChecksum(sum); c != 0 {
		u.SetChecksum(c)
	} else {
		u.SetChecksum(0xffff)
	}
	return b
}
