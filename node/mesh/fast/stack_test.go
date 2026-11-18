//go:build !linux || fastnetstack

package fast

import (
	"context"
	"io"
	"net"
	"net/netip"
	"strconv"
	"sync/atomic"
	"testing"
	"time"

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

// machine is the OS of a node whose mesh interface is the data plane: a stack
// of its own at addr, whose packets go to the data plane's Outbound and, not
// taken, over the "mesh", which here only counts them.
type machine struct {
	stack *stack.Stack
	ep    *linkchan.Endpoint
	mesh  atomic.Int64
	syns  atomic.Int64
}

func newMachine(t *testing.T, addr netip.Addr, svc *Service) *machine {
	t.Helper()
	m := &machine{
		stack: stack.New(stack.Options{
			NetworkProtocols:   []stack.NetworkProtocolFactory{ipv4.NewProtocol},
			TransportProtocols: []stack.TransportProtocolFactory{tcp.NewProtocol, udp.NewProtocol},
		}),
		ep: linkchan.New(1024, 1380, ""),
	}
	if err := m.stack.CreateNIC(1, m.ep); err != nil {
		t.Fatal(err)
	}
	if err := m.stack.AddProtocolAddress(1, tcpip.ProtocolAddress{Protocol: ipv4.ProtocolNumber,
		AddressWithPrefix: tcpip.AddrFrom4(addr.As4()).WithPrefix()}, stack.AddressProperties{}); err != nil {
		t.Fatal(err)
	}
	m.stack.AddRoute(tcpip.Route{Destination: iphdr.IPv4EmptySubnet, NIC: 1})
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		for {
			pkt := m.ep.ReadContext(ctx)
			if pkt == nil {
				return
			}
			v := pkt.ToView()
			pkt.DecRef()
			b := append([]byte(nil), v.AsSlice()...)
			v.Release()
			if !svc.Outbound(b) {
				m.mesh.Add(1)
				if b[9] == 6 && b[33]&0x12 == 0x02 {
					m.syns.Add(1)
				}
			}
		}
	}()
	t.Cleanup(func() {
		cancel()
		<-done
		m.stack.Close()
		m.ep.Close()
	})
	return m
}

// toOS is the data plane's way to the machine.
func (m *machine) toOS(b []byte) {
	pkb := stack.NewPacketBuffer(stack.PacketBufferOptions{Payload: buffer.MakeWithData(b)})
	m.ep.InjectInbound(iphdr.IPv4ProtocolNumber, pkb)
	pkb.DecRef()
}

func (m *machine) dial(ctx context.Context, to netip.AddrPort) (*gonet.TCPConn, error) {
	return gonet.DialContextTCP(ctx, m.stack, tcpip.FullAddress{NIC: 1, Addr: tcpip.AddrFrom4(to.Addr().As4()), Port: to.Port()}, ipv4.ProtocolNumber)
}

// A program on a machine whose capture is the data plane's connects to a
// fast port as to any mesh address: while the channel is not ready its SYN
// goes over the mesh and warms the channel; once ready TCP and UDP are
// answered in the destination's place and carried to it; a packet of a
// connection the mesh carries stays there; a SYN no path takes goes
// unanswered, and its retry over the mesh.
func TestDataPlaneCaptureCarriesTCPAndUDP(t *testing.T) {
	loopback(t)
	key := randKeyB64(t)
	meshPrefix := netip.MustParsePrefix("100.96.0.0/16")
	addrA := netip.MustParseAddr("100.96.0.1")
	addrC := netip.MustParseAddr("100.96.0.3")
	nodes := map[string]netip.Addr{"A": addrA, "C": addrC}

	echo, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer echo.Close()
	go func() {
		for {
			c, err := echo.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				defer c.Close()
				data, _ := io.ReadAll(c)
				_, _ = c.Write(append([]byte("echo:"), data...))
			}(c)
		}
	}()
	tcpPort := uint16(echo.Addr().(*net.TCPAddr).Port)
	udpSrv, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	defer udpSrv.Close()
	go func() {
		buf := make([]byte, 2048)
		for {
			n, from, err := udpSrv.ReadFromUDP(buf)
			if err != nil {
				return
			}
			_, _ = udpSrv.WriteToUDP(append([]byte("udp:"), buf[:n]...), from)
		}
	}()
	udpPort := uint16(udpSrv.LocalAddr().(*net.UDPAddr).Port)

	portC := freeTCPPort(t)
	var svcA, svcC Service
	defer svcA.Close()
	defer svcC.Close()
	isolateHost(t, &svcA)
	isolateHost(t, &svcC)
	svcA.SetHooks(Hooks{Dir: t.TempDir()})
	svcC.SetHooks(Hooks{Dir: t.TempDir()})
	fromA := []netip.Prefix{netip.PrefixFrom(addrA, 32)}
	svcC.Apply(&Config{Port: portC, MeshPort: portC, Keys: []string{key}, Nodes: nodes}, "C", addrC, meshPrefix,
		[]Rule{{Src: fromA, Proto: "any", Ports: [][2]uint16{{tcpPort, tcpPort}, {udpPort, udpPort}}}})
	cfgA := &Config{MeshPort: portC, Keys: []string{key}, Nodes: nodes,
		Targets: []Target{{Node: "C", Port: tcpPort, Proto: "tcp"}, {Node: "C", Port: udpPort, Proto: "udp"}},
		Next:    map[string]string{"C": net.JoinHostPort("127.0.0.1", strconv.Itoa(portC))}}
	svcA.Apply(cfgA, "A", addrA, meshPrefix, nil)

	m := newMachine(t, addrA, &svcA)
	tc, err := newTunCapture(&svcA, m.toOS, svcA.udpSessions)
	if err != nil {
		t.Fatal(err)
	}
	defer tc.close()
	svcA.tun.Store(tc)
	tcpTarget := netip.AddrPortFrom(addrC, tcpPort)

	// Not ready: the SYN goes over the mesh and opens the channel.
	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	_, _ = m.dial(ctx, tcpTarget)
	cancel()
	if m.syns.Load() == 0 {
		t.Fatal("a SYN while the channel is not ready did not go over the mesh")
	}
	if _, ok := channelOf(&svcA, "C"); !ok {
		t.Fatal("the SYN did not open the channel")
	}

	ready := func() {
		chans := svcA.live.Load()
		chans.mu.Lock()
		now := time.Now()
		chans.byDest["C"] = &channel{dest: "C", opened: now, lastUse: now, refreshed: now.Add(time.Hour),
			cands: []*candidate{{plan: Plan{Hops: []string{"C"}, Planned: true}, state: pathReady, since: now, next: now.Add(time.Hour)}}}
		chans.mu.Unlock()
		svcA.readyChanged()
	}
	ready()
	exitsBefore := svcC.Status().Exit.Opened
	for i := range 3 {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		c, err := m.dial(ctx, tcpTarget)
		cancel()
		if err != nil {
			t.Fatalf("dial %d: %v", i, err)
		}
		_ = c.SetDeadline(time.Now().Add(5 * time.Second))
		msg := "hello " + strconv.Itoa(i)
		if _, err := c.Write([]byte(msg)); err != nil {
			t.Fatal(err)
		}
		_ = c.CloseWrite()
		got, _ := io.ReadAll(c)
		_ = c.Close()
		if string(got) != "echo:"+msg {
			t.Fatalf("reply %q", got)
		}
	}
	if n := svcC.Status().Exit.Opened - exitsBefore; n != 3 {
		t.Fatalf("%d streams reached C, want 3", n)
	}

	u, err := gonet.DialUDP(m.stack, nil, &tcpip.FullAddress{NIC: 1, Addr: tcpip.AddrFrom4(addrC.As4()), Port: udpPort}, ipv4.ProtocolNumber)
	if err != nil {
		t.Fatal(err)
	}
	defer u.Close()
	_ = u.SetDeadline(time.Now().Add(5 * time.Second))
	if _, err := u.Write([]byte("ping")); err != nil {
		t.Fatal(err)
	}
	buf := make([]byte, 64)
	n, err := u.Read(buf)
	if err != nil || string(buf[:n]) != "udp:ping" {
		t.Fatalf("UDP reply %q, %v", buf[:n], err)
	}

	// An ACK of a connection nothing took stays on the mesh.
	ack := make([]byte, 40)
	ack[0], ack[9], ack[33] = 0x45, 6, 0x10
	a4, c4 := addrA.As4(), addrC.As4()
	copy(ack[12:16], a4[:])
	copy(ack[16:20], c4[:])
	ack[20], ack[21], ack[22], ack[23] = 0x9c, 0x40, byte(tcpPort>>8), byte(tcpPort)
	if svcA.Outbound(ack) {
		t.Fatal("an ACK of a connection the mesh carries was taken")
	}

	// No path answers: the SYN goes unanswered and its retry over the mesh.
	cfgA.Next = map[string]string{"C": net.JoinHostPort("127.0.0.1", strconv.Itoa(freeTCPPort(t)))}
	svcA.Apply(cfgA, "A", addrA, meshPrefix, nil)
	ready()
	synsBefore := m.syns.Load()
	ctx, cancel = context.WithTimeout(context.Background(), 2500*time.Millisecond)
	_, err = m.dial(ctx, tcpTarget)
	cancel()
	if err == nil {
		t.Fatal("a connection no path takes was answered")
	}
	if m.syns.Load() == synsBefore {
		t.Fatal("the SYN's retry did not go over the mesh")
	}
}
