package health

import (
	"cmp"
	"context"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"net/netip"
	"openbear/node/mesh/clock"
	"os"
	"slices"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"golang.zx2c4.com/wireguard/conn"
	"golang.zx2c4.com/wireguard/device"
	"golang.zx2c4.com/wireguard/tun"
	"openbear/node/mesh/transport"
)

type controlPacket struct {
	data     []byte
	metadata conn.SendMetadata
}
type controlTUN struct {
	owner   *Control
	packets chan controlPacket
	events  chan tun.Event
	done    chan struct{}
	once    sync.Once
}

func (t *controlTUN) File() *os.File           { return nil }
func (t *controlTUN) Name() (string, error)    { return "mesh-control", nil }
func (t *controlTUN) MTU() (int, error)        { return maxMessage + 20, nil }
func (t *controlTUN) BatchSize() int           { return 1 }
func (t *controlTUN) Events() <-chan tun.Event { return t.events }
func (t *controlTUN) Close() error             { t.once.Do(func() { close(t.done) }); return nil }
func (t *controlTUN) Read(bufs [][]byte, sizes []int, offset int) (int, error) {
	return t.ReadWithMetadata(bufs, sizes, offset, make([]conn.SendMetadata, len(bufs)))
}
func (t *controlTUN) ReadWithMetadata(bufs [][]byte, sizes []int, offset int, meta []conn.SendMetadata) (int, error) {
	select {
	case <-t.done:
		return 0, os.ErrClosed
	case p := <-t.packets:
		if len(bufs) == 0 || len(meta) == 0 || len(bufs[0])-offset < len(p.data) {
			return 0, errors.New("control buffer too small")
		}
		sizes[0] = copy(bufs[0][offset:], p.data)
		meta[0] = p.metadata
		return 1, nil
	}
}
func (t *controlTUN) Write(bufs [][]byte, offset int) (int, error) {
	return 0, errors.New("control packet has no authenticated path metadata")
}
func (t *controlTUN) WriteWithMetadata(bufs [][]byte, offset int, meta []conn.ReceiveMetadata) (int, error) {
	c := t.owner
	m := c.manager.Load()
	if m == nil {
		return len(bufs), nil
	}
	for i, b := range bufs {
		if i >= len(meta) || len(b)-offset < 20 {
			continue
		}
		b = b[offset:]
		if b[0] != 0x45 || b[9] != 253 || len(b) > maxMessage+20 || int(binary.BigEndian.Uint16(b[2:4])) != len(b) || binary.BigEndian.Uint16(b[6:8])&0x3fff != 0 {
			continue
		}
		info, ok := c.bind.PathInfo(meta[i].Endpoint)
		if !ok || info.Plane != 1 || !info.ControlAuthenticated {
			continue
		}
		c.mu.RLock()
		n, ok := c.nodes[info.PeerID]
		self := c.address
		c.mu.RUnlock()
		key, keyErr := decodeKey(n.ControlKey)
		if !ok || keyErr != nil || key != meta[i].PublicKey || netip.AddrFrom4([4]byte(b[12:16])) != n.Address || netip.AddrFrom4([4]byte(b[16:20])) != self {
			continue
		}
		payload := append([]byte(nil), b[20:]...)
		if !m.Receive(Received{info.PeerID, Path(info.Ref), clock.Now(), payload}) {
			c.dropped.Add(1)
		}
	}
	return len(bufs), nil
}

type Control struct {
	mu      sync.RWMutex
	bind    *transport.Bind
	dev     *device.Device
	data    *device.Device
	tun     *controlTUN
	manager atomic.Pointer[Manager]
	nodes   map[string]Node
	address netip.Addr
	done    chan struct{}
	once    sync.Once
	wg      sync.WaitGroup
	dropped atomic.Uint64
	punches map[string]punchCall // under mu
}

type punchCall struct {
	ep    conn.Endpoint
	until time.Time
}

func decodeKey(s string) ([32]byte, error) {
	var key [32]byte
	b, err := base64.StdEncoding.DecodeString(s)
	if err != nil || len(b) != 32 {
		return key, errors.New("invalid WireGuard public key")
	}
	copy(key[:], b)
	return key, nil
}
func eligible(c Config) map[string]bool {
	out := map[string]bool{}
	for _, l := range c.Links {
		out[l.Peer] = true
	}
	for i, p := range c.Candidates {
		if c.Mode == "fixed" && i > 0 {
			break
		}
		out[p.Peer] = true
	}
	if c.Role == "relay" {
		for _, n := range c.Nodes {
			for _, r := range n.Relays {
				if r == c.Self {
					out[n.ID] = true
				}
			}
		}
	}
	return out
}

func NewControl(c Config, key string, b *transport.Bind, data *device.Device, logf func(string, ...any)) (*Control, error) {
	secret, err := decodeKey(strings.TrimSpace(key))
	if err != nil {
		return nil, err
	}
	ctl := &Control{bind: b, data: data, nodes: map[string]Node{}, address: c.Address, done: make(chan struct{})}
	ctl.tun = &controlTUN{owner: ctl, packets: make(chan controlPacket, 128), events: make(chan tun.Event, 1), done: make(chan struct{})}
	ctl.dev = device.NewDevice(ctl.tun, b.ControlBind(), &device.Logger{Verbosef: device.DiscardLogf, Errorf: logf})
	ctl.dev.DisableSomeRoamingForBrokenMobileSemantics()
	if err = ctl.dev.IpcSet("private_key=" + hex.EncodeToString(secret[:]) + "\n"); err != nil {
		ctl.Close()
		return nil, err
	}
	if err = ctl.Configure(c); err != nil {
		ctl.Close()
		return nil, err
	}
	return ctl, nil
}

func (c *Control) Configure(cfg Config) error {
	want := eligible(cfg)
	next := map[string]Node{}
	for _, n := range cfg.Nodes {
		if want[n.ID] {
			next[n.ID] = n
		}
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	identities := make([]transport.PeerIdentity, 0, len(next))
	for id, n := range next {
		dk, err := decodeKey(n.PublicKey)
		if err != nil {
			return err
		}
		ck, err := decodeKey(n.ControlKey)
		if err != nil {
			return err
		}
		identities = append(identities, transport.PeerIdentity{ID: id, DataKey: dk, ControlKey: ck})
	}
	if err := c.bind.ValidatePeerSet(identities); err != nil {
		return err
	}
	c.dev.SetPeerLimit(c.bind.DeviceLimits().MaxPeers)
	var uapi strings.Builder
	for id, n := range c.nodes {
		cur, ok := next[id]
		if !ok || cur.PublicKey != n.PublicKey || cur.ControlKey != n.ControlKey {
			k, err := decodeKey(n.ControlKey)
			if err != nil {
				return err
			}
			fmt.Fprintf(&uapi, "public_key=%x\nremove=true\n", k)
			if err = c.bind.RemovePeer(id); err != nil {
				return err
			}
			delete(c.nodes, id)
		}
	}
	for id, n := range next {
		dk, err := decodeKey(n.PublicKey)
		if err != nil {
			return err
		}
		ck, err := decodeKey(n.ControlKey)
		if err != nil {
			return err
		}
		if err = c.bind.RegisterPeer(transport.PeerIdentity{ID: id, DataKey: dk, ControlKey: ck}); err != nil {
			return err
		}
		endpoint, err := c.bind.PeerEndpoint(id)
		if err != nil {
			return err
		}
		c.nodes[id] = n
		fmt.Fprintf(&uapi, "public_key=%x\nendpoint=%s\nreplace_allowed_ips=true\nallowed_ip=%s/32\npersistent_keepalive_interval=0\n", ck, endpoint, n.Address)
	}
	if err := c.dev.IpcSet(uapi.String()); err != nil {
		return err
	}
	c.nodes = next
	c.address = cfg.Address
	return nil
}

// Rebind opens the control plane on the bind again, after the data plane's
// device closed and reopened it (a new UDP listen port).
func (c *Control) Rebind() error { return c.dev.BindUpdate() }

func (c *Control) Start(m *Manager) error {
	c.manager.Store(m)
	if err := c.dev.Up(); err != nil {
		return err
	}
	c.wg.Add(1)
	go func() {
		defer c.wg.Done()
		tick := time.NewTicker(250 * time.Millisecond)
		defer tick.Stop()
		seen := map[Path]bool{}
		for {
			select {
			case <-c.done:
				return
			case <-tick.C:
				current := map[Path]bool{}
				for _, p := range c.bind.Paths() {
					if p.Connected {
						current[Path(p.Ref)] = true
					}
				}
				for p := range seen {
					if !current[p] {
						m.PathChanged(PathEvent{Path: p, Closed: true})
					}
				}
				seen = current
			case e := <-c.bind.Events():
				if e.Kind == "sent" {
					if e.SendID != 0 && !m.Written(WriteResult{e.SendID, e.WriteStartedAt, e.WriteCompletedAt, e.Err}) {
						c.dropped.Add(1)
					}
				}
				if e.Kind == "closed" || e.Kind == "security_error" {
					detail := ""
					if e.Err != nil {
						detail = e.Err.Error()
					}
					m.PathChanged(PathEvent{Path(e.Path.Ref), e.Path.PeerID, e.Kind == "closed", e.Kind == "security_error", detail})
				}
			}
		}
	}()
	return nil
}
func (c *Control) Close() {
	c.once.Do(func() {
		close(c.done)
		if c.dev != nil {
			c.dev.Close()
		}
		c.wg.Wait()
	})
}
func (c *Control) DroppedEvents() uint64 { return c.dropped.Load() + c.bind.Resources().DroppedEvents }
func (c *Control) Prepare(ctx context.Context, p Candidate) (Path, error) {
	if len(p.Pins) > 0 {
		c.bind.SetPins(p.Endpoint, p.Pins)
	}
	ref, err := c.bind.Prepare(ctx, p.Peer, p.Endpoint)
	return Path(ref), err
}
func (c *Control) Send(peer string, path Path, id uint64, payload []byte) error {
	if len(payload) > maxMessage {
		return errors.New("control payload too long")
	}
	ep, err := c.bind.PathEndpoint(transport.PathRef(path))
	if err != nil {
		return err
	}
	c.mu.RLock()
	n, ok := c.nodes[peer]
	addr := c.address
	c.mu.RUnlock()
	if !ok {
		return errors.New("unknown control peer")
	}
	packet := make([]byte, 20+len(payload))
	packet[0] = 0x45
	packet[8] = 64
	packet[9] = 253
	binary.BigEndian.PutUint16(packet[2:4], uint16(len(packet)))
	src, dst := addr.As4(), n.Address.As4()
	copy(packet[12:16], src[:])
	copy(packet[16:20], dst[:])
	copy(packet[20:], payload)
	var sum uint32
	for i := 0; i < 20; i += 2 {
		sum += uint32(binary.BigEndian.Uint16(packet[i : i+2]))
	}
	for sum>>16 != 0 {
		sum = (sum & 0xffff) + (sum >> 16)
	}
	binary.BigEndian.PutUint16(packet[10:12], ^uint16(sum))
	select {
	case <-c.done:
		return os.ErrClosed
	case c.tun.packets <- controlPacket{packet, conn.SendMetadata{Endpoint: ep, ID: id}}:
		return nil
	default:
		return transport.ErrBackpressure
	}
}
func (c *Control) Commit(peer string, path Path) error {
	if err := c.bind.Commit(peer, transport.PathRef(path)); err != nil {
		return err
	}
	c.mu.RLock()
	n, ok := c.nodes[peer]
	c.mu.RUnlock()
	if ok && c.data != nil {
		key, _ := decodeKey(n.PublicKey)
		if p := c.data.LookupPeer(device.NoisePublicKey(key)); p != nil {
			p.SendKeepalive()
		}
	}
	return nil
}
func (c *Control) Deactivate(peer string, path Path) error {
	return c.bind.Deactivate(peer, transport.PathRef(path))
}
func (c *Control) Release(path Path) error {
	if path.ID == "" {
		return nil
	}
	return c.bind.ReleasePath(transport.PathRef(path))
}
func (c *Control) Remove(peer string) error { return c.bind.RemovePeer(peer) }

// Redial moves a dialed TCP or TLS path to a new connection (redial.go),
// and sends a business keepalive over it, so the other end's business
// session authenticates the new path before it is asked to prepare it.
func (c *Control) Redial(ctx context.Context, peer string, path Path) (string, string, error) {
	got, err := c.bind.Redial(ctx, transport.PathRef(path))
	if err != nil {
		return "", "", err
	}
	c.mu.RLock()
	n, ok := c.nodes[peer]
	c.mu.RUnlock()
	if ok && c.data != nil {
		if ep, err := c.bind.PathEndpoint(transport.PathRef(path)); err == nil {
			dk, _ := decodeKey(n.PublicKey)
			if p := c.data.LookupPeer(device.NoisePublicKey(dk)); p != nil {
				p.SendKeepaliveTo(ep)
			}
		}
	}
	return got.From, got.To, nil
}
func (c *Control) Retire(path Path) { c.bind.Retire(transport.PathRef(path)) }
func (c *Control) dataHandshaken(key [32]byte) bool {
	raw, err := c.data.IpcGet()
	if err != nil {
		return false
	}
	match := false
	for _, line := range strings.Split(raw, "\n") {
		k, v, _ := strings.Cut(line, "=")
		if k == "public_key" {
			match = v == hex.EncodeToString(key[:])
		}
		if match && k == "last_handshake_time_sec" && v != "0" {
			return true
		}
	}
	return false
}

func (c *Control) DataReady(peer string, path Path) bool {
	if c.data == nil {
		return true
	}
	c.mu.RLock()
	n, ok := c.nodes[peer]
	c.mu.RUnlock()
	if !ok {
		return false
	}
	ep, err := c.bind.PathEndpoint(transport.PathRef(path))
	if err != nil {
		return false
	}
	info, ok := c.bind.PathInfo(ep)
	key, err := decodeKey(n.PublicKey)
	return ok && info.DataAuthenticated && err == nil && c.dataHandshaken(key)
}

func (c *Control) Handshake(peer string, path Path) error {
	ep, err := c.bind.PathEndpoint(transport.PathRef(path))
	if err != nil {
		return err
	}
	if info, ok := c.bind.PathInfo(ep); ok {
		ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
		_, err = c.bind.Prepare(ctx, peer, info.Endpoint)
		cancel()
		if err != nil {
			return err
		}
	}
	c.mu.RLock()
	n, ok := c.nodes[peer]
	c.mu.RUnlock()
	if !ok {
		return errors.New("unknown peer")
	}
	ck, err := decodeKey(n.ControlKey)
	if err != nil {
		return err
	}
	p := c.dev.LookupPeer(device.NoisePublicKey(ck))
	if p == nil {
		return errors.New("missing control peer")
	}
	err = p.SendHandshakeInitiationTo(false, ep)
	c.Warm(peer, path)
	return err
}

func (c *Control) Warm(peer string, path Path) {
	ep, err := c.bind.PathEndpoint(transport.PathRef(path))
	if err != nil {
		return
	}
	c.mu.RLock()
	n, ok := c.nodes[peer]
	c.mu.RUnlock()
	if !ok {
		return
	}
	dk, _ := decodeKey(n.PublicKey)
	if c.data != nil {
		otherActive := false
		for _, info := range c.bind.Paths() {
			if info.PeerID == peer && info.Active && Path(info.Ref) != path && info.DataAuthenticated {
				otherActive = true
			}
		}
		if p := c.data.LookupPeer(device.NoisePublicKey(dk)); p != nil {
			if !otherActive {
				if err := p.SendHandshakeInitiationTo(false, ep); err != nil {
					if m := c.manager.Load(); m != nil {
						m.PathChanged(PathEvent{Path: path, Peer: peer, Error: "data warm: " + err.Error()})
					}
				}
			}
			p.SendKeepaliveTo(ep)
		}
	}
}

func (c *Control) Punch(peer string, addrs []netip.AddrPort, until time.Time) error {
	if !time.Now().Before(until) {
		return nil
	}
	c.mu.Lock()
	if last, ok := c.punches[peer]; ok && last.until.Equal(until) && time.Now().Before(until) {
		c.mu.Unlock()
		return c.bind.PunchExtend(last.ep, addrs)
	}
	c.mu.Unlock()
	ep, err := c.bind.PunchEndpoint(peer, addrs, until)
	if err != nil {
		return err
	}
	c.mu.Lock()
	if c.punches == nil {
		c.punches = map[string]punchCall{}
	}
	for id, p := range c.punches {
		if time.Now().After(p.until) {
			delete(c.punches, id)
		}
	}
	c.punches[peer] = punchCall{ep, until}
	n, ok := c.nodes[peer]
	c.mu.Unlock()
	if !ok || c.data == nil {
		return errors.New("unknown peer")
	}
	key, err := decodeKey(n.PublicKey)
	if err != nil {
		return err
	}
	p := c.data.LookupPeer(device.NoisePublicKey(key))
	if p == nil {
		return errors.New("missing data peer")
	}
	return p.SendHandshakeInitiationTo(false, ep)
}

func (c *Control) Punched(peer string) []PunchedPath {
	type found struct {
		PunchedPath
		order uint64
	}
	var list []found
	for _, p := range c.bind.Paths() {
		remote, err := netip.ParseAddrPort(p.RemoteAddr)
		if p.PeerID != peer || p.Transport != "udp" || !p.Connected || !p.DataAuthenticated || err != nil {
			continue
		}
		// Path IDs count up as packets first arrive from an address.
		order, _ := strconv.ParseUint(strings.TrimPrefix(p.Ref.ID, "p"), 36, 64)
		list = append(list, found{PunchedPath{Path(p.Ref), netip.AddrPortFrom(remote.Addr().Unmap(), remote.Port()), p.Active}, order})
	}
	slices.SortFunc(list, func(a, b found) int { return cmp.Compare(a.order, b.order) })
	out := make([]PunchedPath, len(list))
	for i, f := range list {
		out[i] = f.PunchedPath
	}
	return out
}

func (c *Control) Reflect(ctx context.Context, server netip.AddrPort, id [8]byte) (netip.AddrPort, error) {
	return c.bind.Reflect(ctx, server, id)
}
func (c *Control) STUN(ctx context.Context, server netip.AddrPort) (netip.AddrPort, error) {
	return c.bind.STUN(ctx, server)
}
func (c *Control) Prime(addrs []netip.AddrPort, ttl int) error { return c.bind.Prime(addrs, ttl) }
func (c *Control) ReflectSeen(id [8]byte) (netip.AddrPort, bool) {
	return c.bind.ReflectSeen(id)
}
func (c *Control) RecentlyFrom(ip netip.Addr, within time.Duration) bool {
	return c.bind.RecentlyFrom(ip, within)
}
func (c *Control) SendTest(target netip.AddrPort, id [8]byte, sender byte) error {
	return c.bind.SendTest(target, id, sender)
}
func (c *Control) Remote(path Path) (netip.AddrPort, bool) {
	for _, p := range c.bind.Paths() {
		if Path(p.Ref) == path {
			a, err := netip.ParseAddrPort(p.RemoteAddr)
			return netip.AddrPortFrom(a.Addr().Unmap(), a.Port()), err == nil
		}
	}
	return netip.AddrPort{}, false
}
func (c *Control) UDPPort() int               { return c.bind.UDPPort() }
func (c *Control) WatchTests(id [8]byte) bool { return c.bind.WatchTests(id) }
func (c *Control) TestsSeen(id [8]byte) byte  { return c.bind.TestsSeen(id) }
func (c *Control) StopTests(id [8]byte)       { c.bind.StopTests(id) }
