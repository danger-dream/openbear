package mesh

import (
	"crypto/ecdh"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"net/netip"
	"strings"
	"testing"
	"time"

	"golang.zx2c4.com/wireguard/conn"
	"golang.zx2c4.com/wireguard/conn/bindtest"
	"golang.zx2c4.com/wireguard/device"
	"golang.zx2c4.com/wireguard/tun/tuntest"
	"openbear/node/mesh/transport"
)

type boundedTestBind struct {
	conn.Bind
	limits conn.DeviceLimits
}

func (b boundedTestBind) DeviceLimits() conn.DeviceLimits { return b.limits }

func TestUnresponsivePeersCannotConsumeHealthySendBudget(t *testing.T) {
	for _, role := range []string{roleMember, roleRelay} {
		cfg := &Config{}
		cfg.Self.Role = role
		b, err := transport.New(transport.Options{ResourceProfile: role})
		if err != nil {
			t.Fatal(err)
		}
		budgets := map[string]conn.DeviceLimits{"data": b.DeviceLimits(), "control": b.ControlBind().DeviceLimits(), "e2e": e2eLimitsFor(cfg).device}
		b.Close()
		for plane, limit := range budgets {
			t.Run(role+"/"+plane, func(t *testing.T) {
				binds := bindtest.NewChannelBinds()
				a, z := tuntest.NewChannelTUN(), tuntest.NewChannelTUN()
				da := device.NewDevice(a.TUN(), boundedTestBind{binds[0], limit}, device.NewLogger(device.LogLevelSilent, ""))
				defer da.Close()
				dz := device.NewDevice(z.TUN(), boundedTestBind{binds[1], limit}, device.NewLogger(device.LogLevelSilent, ""))
				defer dz.Close()
				key := func() *ecdh.PrivateKey {
					k, err := ecdh.X25519().GenerateKey(rand.Reader)
					if err != nil {
						t.Fatal(err)
					}
					return k
				}
				ka, kz := key(), key()
				var config strings.Builder
				fmt.Fprintf(&config, "private_key=%x\npublic_key=%x\nendpoint=127.0.0.1:1\nallowed_ip=10.99.0.250/32\n", ka.Bytes(), kz.PublicKey().Bytes())
				peers := min(limit.MaxPeers-1, limit.TxBuffers/4+1)
				for i := 1; i <= peers; i++ {
					k := key()
					fmt.Fprintf(&config, "public_key=%x\nendpoint=127.0.0.1:1\nallowed_ip=10.99.0.%d/32\n", k.PublicKey().Bytes(), i)
				}
				if err := da.IpcSet(config.String()); err != nil {
					t.Fatal(err)
				}
				if err := dz.IpcSet(fmt.Sprintf("private_key=%x\npublic_key=%x\nendpoint=127.0.0.1:2\nallowed_ip=10.99.1.1/32\n", kz.Bytes(), ka.PublicKey().Bytes())); err != nil {
					t.Fatal(err)
				}
				if err := da.Up(); err != nil {
					t.Fatal(err)
				}
				if err := dz.Up(); err != nil {
					t.Fatal(err)
				}
				send := func(dst string) {
					t.Helper()
					select {
					case a.Outbound <- tuntest.Ping(netip.MustParseAddr(dst), netip.MustParseAddr("10.99.1.1")):
					case <-time.After(time.Second):
						t.Fatal("shared TUN reader stalled")
					}
				}
				receive := func() {
					t.Helper()
					select {
					case <-z.Inbound:
					case <-time.After(2 * time.Second):
						t.Fatal("healthy peer blocked")
					}
				}
				send("10.99.0.250")
				receive()
				for i := 1; i <= peers; i++ {
					for range 8 {
						send(fmt.Sprintf("10.99.0.%d", i))
					}
				}
				send("10.99.0.250")
				receive()
			})
		}
	}
}

func TestFixedDeviceCanRaisePeerAdmissionWithoutRebuilding(t *testing.T) {
	b := boundedTestBind{bindtest.NewChannelBinds()[0], conn.DeviceLimits{MaxPeers: 16, Workers: 1, BatchSize: 1, QueueDepth: 4, TxBuffers: 32, RxBuffers: 32, KeepaliveBuffers: 4}}
	d := device.NewDevice(tuntest.NewChannelTUN().TUN(), b, device.NewLogger(device.LogLevelSilent, ""))
	defer d.Close()
	for i := 1; i <= 16; i++ {
		if _, err := d.NewPeer(device.NoisePublicKey{byte(i)}); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := d.NewPeer(device.NoisePublicKey{17}); err == nil {
		t.Fatal("initial bound missing")
	}
	d.SetPeerLimit(128)
	for i := 17; i <= 128; i++ {
		if _, err := d.NewPeer(device.NoisePublicKey{byte(i)}); err != nil {
			t.Fatalf("peer %d: %v", i, err)
		}
	}
	if _, err := d.NewPeer(device.NoisePublicKey{129}); err == nil {
		t.Fatal("relay bound missing")
	}
}

type failingHost struct {
	hostNet
	want  hostConfig
	fail  bool
	calls int
}

func (h *failingHost) apply(c hostConfig) error {
	h.calls++
	h.want = c
	if h.fail {
		h.fail = false
		return errors.New("partial host mutation")
	}
	return nil
}

func TestEngineDoesNotSkipHostRollback(t *testing.T) {
	l := newLab(t, twoMembers(allowICMP, allowICMP))
	nodes := l.up()
	a := nodes["A"]
	a.mu.Lock()
	old := a.hostCfg
	h := &failingHost{hostNet: a.host, want: old, fail: true}
	a.host = h
	a.mu.Unlock()
	next := l.config("A")
	next.Revision = 2
	next.Network.MTU = 1280
	if err := a.applyConfig(next); err == nil {
		t.Fatal("host failure ignored")
	}
	if !h.want.equal(old) || h.calls < 2 || a.hostDirty {
		t.Fatal("rollback skipped", h.calls, h.want)
	}
}

func TestRoleUpgradeRegistersMoreThanMemberPeerLimit(t *testing.T) {
	l := newHealthLab(t, spec{relays: []string{"R"}, members: map[string][]string{"H": {"R"}}, rules: map[string][]Rule{"R": allowICMP, "H": allowICMP}})
	h := l.startHealthNode("H")
	next := l.config("H")
	next.Revision = 2
	next.Self.Role = roleRelay
	next.Self.Resources = roleRelay
	next.Entry = nil
	for i := 0; i < 20; i++ {
		id := fmt.Sprintf("N%d", i)
		_, pub := newKey(t)
		_, control := newKey(t)
		next.Nodes = append(next.Nodes, Node{ID: id, Role: roleMember, Address: netip.MustParseAddr(fmt.Sprintf("100.96.0.%d", 100+i)), ControlAddress: netip.MustParseAddr(fmt.Sprintf("100.96.255.%d", 100+i)), PublicKey: pub, ControlKey: control, SignKey: testSignPub(id), E2EKey: testE2EPub(id), Relays: []string{"H"}})
	}
	if err := h.applyConfig(next); err != nil {
		t.Fatal(err)
	}
	resources := h.status().Resources
	if resources.Profile != roleMember || resources.PendingProfile != roleRelay {
		t.Fatal(resources)
	}
	if h.e2e.status().Limit != 128 {
		t.Fatal("inner peer admission stayed in member profile")
	}
	// Rejection before mutation leaves all previously registered peers usable.
	bad := *next
	bad.Revision++
	bad.Nodes = append([]Node(nil), next.Nodes...)
	bad.Nodes[len(bad.Nodes)-1].ControlKey = "bad"
	if err := h.applyConfig(&bad); err == nil {
		t.Fatal("invalid peer accepted")
	}
	if _, err := h.bind.(*streamBind).PeerEndpoint("N19"); err != nil {
		t.Fatal("old peer removed on failed config", err)
	}
	_, peers := deviceState(h.dev)
	raw, _ := keyHex(next.Nodes[len(next.Nodes)-1].PublicKey)
	if _, ok := peers[raw]; !ok {
		t.Fatal("data peer missing", hex.EncodeToString([]byte(raw)))
	}
}
