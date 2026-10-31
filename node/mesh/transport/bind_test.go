package transport

import (
	"bytes"
	"crypto/rand"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"net"
	"net/netip"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"golang.org/x/crypto/curve25519"
	"golang.zx2c4.com/wireguard/conn"
	"golang.zx2c4.com/wireguard/device"
	"golang.zx2c4.com/wireguard/tun/tuntest"
)

func freePort(t *testing.T, network string) int {
	t.Helper()
	if network == "udp" {
		c, err := net.ListenUDP("udp", &net.UDPAddr{})
		if err != nil {
			t.Fatal(err)
		}
		defer c.Close()
		return c.LocalAddr().(*net.UDPAddr).Port
	}
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	return l.Addr().(*net.TCPAddr).Port
}
func newBind(t *testing.T, o Options) *Bind {
	t.Helper()
	b, err := New(o)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { b.Close() })
	return b
}
func waitUntil(t *testing.T, d time.Duration, what string, f func() bool) {
	t.Helper()
	end := time.Now().Add(d)
	for !f() {
		if time.Now().After(end) {
			t.Fatal("timeout: " + what)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

type testWG struct {
	dev    *device.Device
	tun    *tuntest.ChannelTUN
	public string
	ip     netip.Addr
}

func wg(t *testing.T, bind conn.Bind, ip string) *testWG {
	t.Helper()
	var key [32]byte
	rand.Read(key[:])
	pub, _ := curve25519.X25519(key[:], curve25519.Basepoint)
	tun := tuntest.NewChannelTUN()
	dev := device.NewDevice(tun.TUN(), bind, device.NewLogger(device.LogLevelError, "transport test: "))
	dev.DisableSomeRoamingForBrokenMobileSemantics()
	if err := dev.IpcSet("private_key=" + hex.EncodeToString(key[:]) + "\n"); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(dev.Close)
	return &testWG{dev: dev, tun: tun, public: hex.EncodeToString(pub), ip: netip.MustParseAddr(ip)}
}
func addPeer(t *testing.T, from, to *testWG, endpoint string) {
	t.Helper()
	s := fmt.Sprintf("public_key=%s\nallowed_ip=%s/32\npersistent_keepalive_interval=1\n", to.public, to.ip)
	if endpoint != "" {
		s += "endpoint=" + endpoint + "\n"
	}
	if err := from.dev.IpcSet(s); err != nil {
		t.Fatal(err)
	}
}
func dataPacket(from, to *testWG, payload []byte) []byte {
	p := make([]byte, 20+len(payload))
	p[0] = 0x45
	p[8] = 64
	p[9] = 253
	binary.BigEndian.PutUint16(p[2:], uint16(len(p)))
	copy(p[12:16], from.ip.AsSlice())
	copy(p[16:20], to.ip.AsSlice())
	copy(p[20:], payload)
	return p
}
func transfer(t *testing.T, from, to *testWG, label string) {
	t.Helper()
	want := dataPacket(from, to, []byte(label))
	deadline := time.Now().Add(12 * time.Second)
	for {
		from.tun.Outbound <- want
		select {
		case p := <-to.tun.Inbound:
			if bytes.Equal(p, want) {
				return
			}
		case <-time.After(50 * time.Millisecond):
		}
		if time.Now().After(deadline) {
			t.Fatalf("%s not delivered: from=%s to=%s", label, wgPacketState(from), wgPacketState(to))
		}
	}
}
func wgPacketState(n *testWG) string {
	state, _ := n.dev.IpcGet()
	var result []string
	for _, line := range strings.Split(state, "\n") {
		name, _, _ := strings.Cut(line, "=")
		switch name {
		case "endpoint", "last_handshake_time_sec", "last_handshake_time_nsec", "rx_bytes", "tx_bytes":
			result = append(result, line)
		}
	}
	return strings.Join(result, ",")
}
func peerEndpointOf(t *testing.T, n *testWG) string {
	t.Helper()
	s, err := n.dev.IpcGet()
	if err != nil {
		t.Fatal(err)
	}
	for _, line := range strings.Split(s, "\n") {
		if strings.HasPrefix(line, "endpoint=") {
			return strings.TrimPrefix(line, "endpoint=")
		}
	}
	return ""
}

func TestWireGuardTransportsAndReconnect(t *testing.T) {
	for _, transport := range []string{"udp", "tcp", "tls"} {
		t.Run(transport, func(t *testing.T) {
			port := freePort(t, "tcp")
			o := Options{MTU: 1280}
			switch transport {
			case "udp":
				port = freePort(t, "udp")
				o.ListenUDP = port
			case "tcp":
				o.ListenTCP = port
			case "tls":
				o.ListenTLS = port
				o.TLSDir = t.TempDir()
			}
			server := newBind(t, o)
			client := newBind(t, Options{MTU: 1280})
			endpoint := fmt.Sprintf("%s://127.0.0.1:%d", transport, port)
			if transport == "udp" {
				endpoint = fmt.Sprintf("udp://localhost:%d", port)
			}
			if transport == "tls" {
				c, err := EnsureCertificate(o.TLSDir)
				if err != nil {
					t.Fatal(err)
				}
				client.SetPins(endpoint, []string{c.Active.FingerprintSHA256})
			}
			a := wg(t, client, "10.99.0.1")
			z := wg(t, server, "10.99.0.2")
			addPeer(t, z, a, "")
			addPeer(t, a, z, endpoint)
			if err := z.dev.Up(); err != nil {
				t.Fatal(err)
			}
			if err := a.dev.Up(); err != nil {
				t.Fatal(err)
			}
			transfer(t, a, z, "request")
			transfer(t, z, a, "reply")
			if stats := client.Stats(); len(stats) != 1 || stats[0].RxBytes == 0 || stats[0].TxBytes == 0 {
				t.Fatalf("path counters: %+v", stats)
			}
			if got := peerEndpointOf(t, a); got != endpoint {
				t.Fatalf("endpoint %q", got)
			}
			if !strings.HasPrefix(peerEndpointOf(t, z), transport+"://") {
				t.Fatal("passive peer did not learn transport")
			}
			if transport != "udp" {
				client.mu.Lock()
				ep := client.paths[endpoint]
				old := ep.current
				client.mu.Unlock()
				if old == nil {
					t.Fatal("missing connection")
				}
				old.conn.Close()
				waitUntil(t, 3*time.Second, "reconnect", func() bool { client.mu.Lock(); defer client.mu.Unlock(); return ep.current != nil && ep.current != old })
				transfer(t, a, z, "after reconnect")
				transfer(t, z, a, "return reconnect")
				if client.Stats()[0].Reconnects == 0 {
					t.Fatal("reconnect counter")
				}
				server.mu.Lock()
				for s := range server.run.sessions {
					if !s.authenticated {
						t.Fatal("WG reply did not confirm passive identity")
					}
				}
				server.mu.Unlock()
			}
		})
	}
}

type captured struct {
	p  []byte
	ep conn.Endpoint
}
type holdBind struct {
	conn.Bind
	hold atomic.Bool
	held chan captured
}

func (b *holdBind) Send(p [][]byte, e conn.Endpoint) error {
	if b.hold.Load() && strings.HasPrefix(e.DstToString(), "udp://") {
		for _, v := range p {
			if len(v) > 32 && binary.LittleEndian.Uint32(v[:4]) == 4 {
				select {
				case b.held <- captured{append([]byte(nil), v...), e}:
				default:
				}
			}
		}
		return nil
	}
	return b.Bind.Send(p, e)
}
func TestLateUDPCannotReplaceFixedStream(t *testing.T) {
	for _, transport := range []string{"tcp", "tls"} {
		t.Run(transport, func(t *testing.T) {
			udp, tcp := freePort(t, "udp"), freePort(t, "tcp")
			o := Options{ListenUDP: udp}
			if transport == "tcp" {
				o.ListenTCP = tcp
			} else {
				o.ListenTLS = tcp
				o.TLSDir = t.TempDir()
			}
			server := newBind(t, o)
			hold := &holdBind{Bind: server, held: make(chan captured, 4)}
			client := newBind(t, Options{})
			a := wg(t, client, "10.99.0.1")
			z := wg(t, hold, "10.99.0.2")
			udpEP := fmt.Sprintf("udp://127.0.0.1:%d", udp)
			streamEP := fmt.Sprintf("%s://127.0.0.1:%d", transport, tcp)
			addPeer(t, z, a, "")
			addPeer(t, a, z, udpEP)
			z.dev.Up()
			a.dev.Up()
			transfer(t, a, z, "initial")
			transfer(t, z, a, "initial reply")
			hold.hold.Store(true)
			late := dataPacket(z, a, []byte("late real ciphertext"))
			z.tun.Outbound <- late
			var captured captured
			select {
			case captured = <-hold.held:
			case <-time.After(time.Second):
				t.Fatal("no captured UDP")
			}
			if transport == "tls" {
				cert, err := EnsureCertificate(o.TLSDir)
				if err != nil {
					t.Fatal(err)
				}
				client.SetPins(streamEP, []string{cert.Active.FingerprintSHA256})
			}
			if err := a.dev.IpcSet("public_key=" + z.public + "\nendpoint=" + streamEP + "\n"); err != nil {
				t.Fatal(err)
			}
			transfer(t, a, z, "stream selected")
			hold.hold.Store(false)
			if err := server.Send([][]byte{captured.p}, captured.ep); err != nil {
				t.Fatal(err)
			}
			select {
			case got := <-a.tun.Inbound:
				if !bytes.Equal(got, late) {
					t.Fatal("wrong late packet")
				}
			case <-time.After(time.Second):
				t.Fatal("late UDP was not authenticated/delivered")
			}
			if peerEndpointOf(t, a) != streamEP {
				t.Fatal("late UDP changed selected stream")
			}
			transfer(t, a, z, "still stream")
			transfer(t, z, a, "return still stream")
			client.Release(udpEP)
		})
	}
}

func TestPassiveUDPNATLearnsNewPort(t *testing.T) {
	server := newBind(t, Options{ListenUDP: freePort(t, "udp")})
	client := newBind(t, Options{ListenUDP: freePort(t, "udp")})
	a := wg(t, client, "10.99.0.1")
	z := wg(t, server, "10.99.0.2")
	addPeer(t, z, a, "")
	addPeer(t, a, z, fmt.Sprintf("udp://127.0.0.1:%d", server.opt.ListenUDP))
	z.dev.Up()
	a.dev.Up()
	transfer(t, a, z, "before NAT")
	transfer(t, z, a, "before NAT reply")
	before := peerEndpointOf(t, z)
	if err := client.Reconfigure(Options{ListenUDP: freePort(t, "udp")}); err != nil {
		t.Fatal(err)
	}
	transfer(t, a, z, "after NAT")
	transfer(t, z, a, "after NAT reply")
	if peerEndpointOf(t, z) == before {
		t.Fatal("passive endpoint did not roam")
	}
}
