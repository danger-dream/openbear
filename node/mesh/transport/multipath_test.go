package transport

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"net"
	"net/netip"
	"sort"
	"sync"
	"testing"
	"time"

	"golang.org/x/crypto/curve25519"
	"golang.zx2c4.com/wireguard/conn"
	"golang.zx2c4.com/wireguard/device"
	"golang.zx2c4.com/wireguard/tun"
	"golang.zx2c4.com/wireguard/tun/tuntest"
	"openbear/node/mesh/wire"
)

type metaPacket struct {
	data []byte
	send conn.SendMetadata
	recv conn.ReceiveMetadata
}
type metadataTUN struct {
	tun.Device
	in, out chan metaPacket
	stop    chan struct{}
	once    sync.Once
}

func newMetadataTUN() *metadataTUN {
	return &metadataTUN{Device: tuntest.NewChannelTUN().TUN(), in: make(chan metaPacket, 64), out: make(chan metaPacket, 64), stop: make(chan struct{})}
}
func (m *metadataTUN) Read(b [][]byte, s []int, o int) (int, error) {
	return m.ReadWithMetadata(b, s, o, make([]conn.SendMetadata, len(b)))
}
func (m *metadataTUN) ReadWithMetadata(b [][]byte, s []int, o int, metadata []conn.SendMetadata) (int, error) {
	select {
	case <-m.stop:
		return 0, net.ErrClosed
	case p := <-m.out:
		s[0] = copy(b[0][o:], p.data)
		metadata[0] = p.send
		return 1, nil
	}
}
func (m *metadataTUN) Write(b [][]byte, o int) (int, error) {
	return m.WriteWithMetadata(b, o, make([]conn.ReceiveMetadata, len(b)))
}
func (m *metadataTUN) WriteWithMetadata(b [][]byte, o int, metadata []conn.ReceiveMetadata) (int, error) {
	for i, v := range b {
		p := metaPacket{data: append([]byte(nil), v[o:]...), recv: metadata[i]}
		select {
		case <-m.stop:
			return 0, net.ErrClosed
		case m.in <- p:
		}
	}
	return len(b), nil
}
func (m *metadataTUN) Close() error {
	m.once.Do(func() { close(m.stop); m.Device.Close() })
	return nil
}

type controlWG struct {
	dev    *device.Device
	tun    *metadataTUN
	public [32]byte
	ip     netip.Addr
}

func controlDevice(t *testing.T, b conn.Bind, ip string) *controlWG {
	t.Helper()
	var key [32]byte
	rand.Read(key[:])
	pub, _ := curve25519.X25519(key[:], curve25519.Basepoint)
	tun := newMetadataTUN()
	d := device.NewDevice(tun, b, device.NewLogger(device.LogLevelSilent, ""))
	d.DisableSomeRoamingForBrokenMobileSemantics()
	if err := d.IpcSet("private_key=" + hex.EncodeToString(key[:]) + "\n"); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(d.Close)
	n := &controlWG{dev: d, tun: tun, ip: netip.MustParseAddr(ip)}
	copy(n.public[:], pub)
	return n
}
func addControlPeer(t *testing.T, from, to *controlWG, endpoint string) {
	t.Helper()
	s := fmt.Sprintf("public_key=%x\nallowed_ip=%s/32\nendpoint=%s\n", to.public, to.ip, endpoint)
	if err := from.dev.IpcSet(s); err != nil {
		t.Fatal(err)
	}
}
func key32(s string) [32]byte {
	raw, _ := hex.DecodeString(s)
	var k [32]byte
	copy(k[:], raw)
	return k
}
func controlPacket(from, to *controlWG, label string) []byte {
	return dataPacket(&testWG{ip: from.ip}, &testWG{ip: to.ip}, []byte(label))
}
func sendControl(t *testing.T, from, to *controlWG, b *Bind, ref PathRef, label string, seq uint64) metaPacket {
	t.Helper()
	ep, err := b.PathEndpoint(ref)
	if err != nil {
		t.Fatal(err)
	}
	want := controlPacket(from, to, label)
	deadline := time.NewTimer(8 * time.Second)
	defer deadline.Stop()
	ticker := time.NewTicker(100 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case from.tun.out <- metaPacket{data: want, send: conn.SendMetadata{Endpoint: ep, ID: seq}}:
		default:
		}
		select {
		case p := <-to.tun.in:
			if bytes.Equal(p.data, want) {
				return p
			}
		case <-ticker.C:
		case <-deadline.C:
			t.Fatal("control packet not delivered: " + label)
		}
	}
}

type pathLab struct {
	a, z          *Bind
	ad, zd        *testWG
	ac, zc        *controlWG
	tap           *tapPlane
	ztap          *tapPlane
	tcp, udp, tls string
}

func newPathLab(t *testing.T) *pathLab {
	t.Helper()
	return newPathLabSpeaking(t, wire.Range{}, wire.Range{})
}

// newPathLabSpeaking: z speaks mesh protocols zp, a ap (zero: this build's).
func newPathLabSpeaking(t *testing.T, zp, ap wire.Range) *pathLab {
	t.Helper()
	z := newBind(t, Options{ListenTCP: freePort(t, "tcp"), ListenUDP: freePort(t, "udp"), ListenTLS: freePort(t, "tcp"), TLSDir: t.TempDir(), Protocol: zp})
	a := newBind(t, Options{Protocol: ap})
	ad, zd := wg(t, a, "10.99.0.1"), wg(t, z, "10.99.0.2")
	tap := &tapPlane{PlaneBind: a.ControlBind()}
	ztap := &tapPlane{PlaneBind: z.ControlBind()}
	ac, zc := controlDevice(t, tap, "100.96.255.1"), controlDevice(t, ztap, "100.96.255.2")
	if err := a.RegisterPeer(PeerIdentity{ID: "z", DataKey: key32(zd.public), ControlKey: zc.public}); err != nil {
		t.Fatal(err)
	}
	if err := z.RegisterPeer(PeerIdentity{ID: "a", DataKey: key32(ad.public), ControlKey: ac.public}); err != nil {
		t.Fatal(err)
	}
	ae, _ := a.PeerEndpoint("z")
	ze, _ := z.PeerEndpoint("a")
	// ChannelTUN emits EventUp on creation. Do not briefly enable persistent
	// keepalive via addPeer: an already-up device would immediately initiate a
	// handshake before any data path is committed. Setting the interval back
	// to zero does not cancel that handshake's retransmit timer.
	for _, cfg := range []struct {
		from, to *testWG
		endpoint string
	}{{ad, zd, ae}, {zd, ad, ze}} {
		if err := cfg.from.dev.IpcSet(fmt.Sprintf("public_key=%s\nallowed_ip=%s/32\nendpoint=%s\npersistent_keepalive_interval=0\n", cfg.to.public, cfg.to.ip, cfg.endpoint)); err != nil {
			t.Fatal(err)
		}
	}
	addControlPeer(t, ac, zc, ae)
	addControlPeer(t, zc, ac, ze)
	for _, d := range []*device.Device{zd.dev, ad.dev, zc.dev, ac.dev} {
		if err := d.Up(); err != nil {
			t.Fatal(err)
		}
	}
	tlsURI := fmt.Sprintf("tls://127.0.0.1:%d", z.opt.ListenTLS)
	cert, _ := EnsureCertificate(z.opt.TLSDir)
	a.SetPins(tlsURI, []string{cert.Active.FingerprintSHA256})
	return &pathLab{ztap: ztap, tap: tap, a: a, z: z, ad: ad, zd: zd, ac: ac, zc: zc, tcp: fmt.Sprintf("tcp://127.0.0.1:%d", z.opt.ListenTCP), udp: fmt.Sprintf("udp://127.0.0.1:%d", z.opt.ListenUDP), tls: tlsURI}
}
func (l *pathLab) prepare(t *testing.T, uri string) (PathRef, PathRef) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	ref, err := l.a.Prepare(ctx, "z", uri)
	if err != nil {
		t.Fatal(err)
	}
	p := sendControl(t, l.ac, l.zc, l.a, ref, "challenge-"+uri, 10)
	info, ok := l.z.PathInfo(p.recv.Endpoint)
	if !ok || info.PeerID != "a" || p.recv.PublicKey != l.ac.public || info.Plane != 1 {
		t.Fatalf("bad control provenance: %+v %+v", info, p.recv)
	}
	ack := sendControl(t, l.zc, l.ac, l.z, info.Ref, "answer-"+uri, 11)
	back, ok := l.a.PathInfo(ack.recv.Endpoint)
	if !ok || back.Ref != ref || back.PeerID != "z" {
		t.Fatalf("reply wrong path %+v", back)
	}
	return ref, info.Ref
}
func (l *pathLab) commit(t *testing.T, a, z PathRef) {
	t.Helper()
	if err := l.a.Commit("z", a); err != nil {
		t.Fatal(err)
	}
	if err := l.z.Commit("a", z); err != nil {
		t.Fatal(err)
	}
	transfer(t, l.ad, l.zd, "data forward")
	transfer(t, l.zd, l.ad, "data return")
}

func TestMultipathControlPrepareCommit(t *testing.T) {
	l := newPathLab(t)
	active, remote := l.prepare(t, l.tcp)
	l.commit(t, active, remote)
	l.a.mu.Lock()
	old := l.a.ids[active.ID].current
	l.a.mu.Unlock()
	backup, backRemote := l.prepare(t, l.tls)
	if err := l.a.ReleasePath(active); err == nil {
		t.Fatal("active path released")
	}
	if got := peerEndpointOf(t, l.ad); got != "mesh-peer://z" {
		t.Fatal("logical endpoint replaced", got)
	}
	l.a.mu.Lock()
	unchanged := l.a.peers["z"].active == active && l.a.ids[active.ID].current == old
	l.a.mu.Unlock()
	if !unchanged {
		t.Fatal("probe disturbed active path")
	}
	transfer(t, l.ad, l.zd, "still original path")
	l.commit(t, backup, backRemote)
	if err := l.a.ReleasePath(active); err != nil {
		t.Fatal(err)
	}
	if err := l.z.ReleasePath(remote); err != nil {
		t.Fatal(err)
	}
	if _, err := l.a.PathEndpoint(active); err == nil {
		t.Fatal("retired reference accepted")
	}
	l.a.mu.Lock()
	now := l.a.ids[backup.ID].current
	l.a.mu.Unlock()
	now.conn.Close()
	waitUntil(t, 3*time.Second, "new generation", func() bool {
		l.a.mu.Lock()
		defer l.a.mu.Unlock()
		return l.a.ids[backup.ID].generation > backup.Generation
	})
	if err := l.a.Commit("z", backup); err == nil {
		t.Fatal("stale generation commit")
	}
	if err := l.a.RemovePeer("z"); err != nil {
		t.Fatal(err)
	}
	if len(l.a.Paths()) != 0 {
		t.Fatalf("peer paths retained: %+v", l.a.Paths())
	}
}

func TestHundredFailedProbesPreserveActivePath(t *testing.T) {
	l := newPathLab(t)
	active, remote := l.prepare(t, l.tcp)
	l.commit(t, active, remote)
	l.a.mu.Lock()
	original := l.a.ids[active.ID].current
	l.a.mu.Unlock()
	sink, err := net.ListenUDP("udp", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	defer sink.Close()
	uri := "udp://" + sink.LocalAddr().String()
	var elapsed []time.Duration
	for i := 0; i < 100; i++ {
		ref, err := l.a.Prepare(context.Background(), "z", uri)
		if err != nil {
			t.Fatal(err)
		}
		ep, _ := l.a.PathEndpoint(ref)
		l.ac.tun.out <- metaPacket{data: controlPacket(l.ac, l.zc, "no response"), send: conn.SendMetadata{Endpoint: ep, ID: uint64(100 + i)}}
		start := time.Now()
		transfer(t, l.ad, l.zd, fmt.Sprintf("probe-%d", i))
		elapsed = append(elapsed, time.Since(start))
		if err := l.a.ReleasePath(ref); err != nil {
			t.Fatal(err)
		}
	}
	sort.Slice(elapsed, func(i, j int) bool { return elapsed[i] < elapsed[j] })
	if elapsed[94] > 50*time.Millisecond {
		t.Fatalf("local probe p95 %s", elapsed[94])
	}
	l.a.mu.Lock()
	same := l.a.ids[active.ID].current == original && l.a.peers["z"].active == active
	l.a.mu.Unlock()
	if !same {
		t.Fatal("failed probes changed active connection")
	}
	t.Logf("100 failed path probes, local data p95=%s", elapsed[94])
}

func TestFailedPrepareReclaimsTasks(t *testing.T) {
	l := newPathLab(t)
	active, remote := l.prepare(t, l.tcp)
	l.commit(t, active, remote)
	unavailable := fmt.Sprintf("tcp://127.0.0.1:%d", freePort(t, "tcp"))
	for i := 0; i < 100; i++ {
		ctx, cancel := context.WithTimeout(context.Background(), 12*time.Millisecond)
		_, err := l.a.Prepare(ctx, "z", unavailable)
		cancel()
		if err == nil {
			t.Fatal("unreachable prepare succeeded")
		}
		l.a.mu.Lock()
		_, leaked := l.a.paths[unavailable]
		l.a.mu.Unlock()
		if leaked {
			t.Fatal("failed prepare left a dial task")
		}
	}
	transfer(t, l.ad, l.zd, "after 100 failed prepares")
}

func TestHotPathSwitchTransportLatency(t *testing.T) {
	l := newPathLab(t)
	a, ar := l.prepare(t, l.tcp)
	l.commit(t, a, ar)
	z, zr := l.prepare(t, l.tls)
	var samples []time.Duration
	for i := 0; i < 20; i++ {
		start := time.Now()
		l.commit(t, z, zr)
		samples = append(samples, time.Since(start))
		a, z = z, a
		ar, zr = zr, ar
	}
	sort.Slice(samples, func(i, j int) bool { return samples[i] < samples[j] })
	t.Logf("20 local ready-path commits + bidirectional data, p95=%s (excludes health detection/route propagation)", samples[18])
	if samples[18] > 8*time.Second {
		t.Fatal("ready path commit too slow")
	}
}

func TestConcurrentPrepareQuotaAndControlGeneration(t *testing.T) {
	b := newBind(t, Options{})
	openBind(t, b)
	var data, control [32]byte
	data[0] = 1
	control[0] = 2
	if err := b.RegisterPeer(PeerIdentity{ID: "z", DataKey: data, ControlKey: control}); err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	results := make(chan error, 8)
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			_, err := b.Prepare(context.Background(), "z", fmt.Sprintf("udp://127.0.0.1:%d", 50000+i))
			results <- err
		}(i)
	}
	wg.Wait()
	close(results)
	success := 0
	for err := range results {
		if err == nil {
			success++
		}
	}
	if success != 3 || len(b.Paths()) != 3 {
		t.Fatalf("per-peer quota: success=%d paths=%d", success, len(b.Paths()))
	}
	old, newer := b.ControlBind(), b.ControlBind()
	if _, _, err := old.Open(0); err != nil {
		t.Fatal(err)
	}
	old.Close()
	if _, _, err := newer.Open(0); err != nil {
		t.Fatal(err)
	}
	old.Close()
	b.mu.Lock()
	alive := b.run.control == newer.run && newer.run.ctx.Err() == nil
	b.mu.Unlock()
	if !alive {
		t.Fatal("stale control close killed new generation")
	}
	newer.Close()
	b.RemovePeer("z")
	if len(b.Paths()) != 0 {
		t.Fatal("revocation left paths")
	}
}
