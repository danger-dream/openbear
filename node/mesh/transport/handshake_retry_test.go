package transport

import (
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"net"
	"strings"
	"sync"
	"testing"
	"time"

	"golang.zx2c4.com/wireguard/conn"
	"golang.zx2c4.com/wireguard/device"
)

type handshakeTap struct {
	*Bind
	mu       sync.Mutex
	drop     bool
	failure  error
	attempts []conn.Endpoint
}

func (b *handshakeTap) SendWithMetadata(bufs [][]byte, key [32]byte, m conn.SendMetadata) error {
	if len(bufs) == 1 && len(bufs[0]) == 148 && binary.LittleEndian.Uint32(bufs[0]) == 1 {
		b.mu.Lock()
		b.attempts = append(b.attempts, m.Endpoint)
		drop, err := b.drop, b.failure
		b.drop = false
		b.mu.Unlock()
		if drop {
			return err
		}
	}
	return b.Bind.SendWithMetadata(bufs, key, m)
}

func TestFailedDataProbesKeepActiveTCPAndSession(t *testing.T) {
	l := newPathLab(t)
	active, remote := l.prepare(t, l.tcp)
	prewarmData(t, l, active)
	l.commit(t, active, remote)
	beforeA, beforeZ := wgPacketState(l.ad), wgPacketState(l.zd)
	stamp := func(n *testWG) string {
		var result string
		for _, field := range strings.Split(wgPacketState(n), ",") {
			if strings.HasPrefix(field, "last_handshake_time_") {
				result += field
			}
		}
		return result
	}
	handshakeA, handshakeZ := stamp(l.ad), stamp(l.zd)
	l.a.mu.Lock()
	original := l.a.ids[active.ID].current
	l.a.mu.Unlock()
	sink, err := net.ListenUDP("udp", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	defer sink.Close()
	ref, err := l.a.Prepare(context.Background(), "z", "udp://"+sink.LocalAddr().String())
	if err != nil {
		t.Fatal(err)
	}
	ep, err := l.a.PathEndpoint(ref)
	if err != nil {
		t.Fatal(err)
	}
	peer := l.ad.dev.LookupPeer(device.NoisePublicKey(key32(l.zd.public)))
	for i := 0; i < 100; i++ {
		peer.SendKeepaliveTo(ep)
		transfer(t, l.ad, l.zd, fmt.Sprintf("failed-data-probe-%d", i))
	}
	if err := l.a.ReleasePath(ref); err != nil {
		t.Fatal(err)
	}
	l.a.mu.Lock()
	unchanged := l.a.peers["z"].active == active && l.a.ids[active.ID].current == original
	l.a.mu.Unlock()
	if !unchanged || stamp(l.ad) != handshakeA || stamp(l.zd) != handshakeZ {
		t.Fatalf("failed data probes changed TCP/session: before=%s/%s after=%s/%s", beforeA, beforeZ, wgPacketState(l.ad), wgPacketState(l.zd))
	}
	transfer(t, l.zd, l.ad, "reverse-after-failed-data-probes")
}

func TestExplicitHandshakeRetryRetainsUncommittedPath(t *testing.T) {
	for _, mode := range []string{"lost-first", "local-backpressure", "already-throttled"} {
		t.Run(mode, func(t *testing.T) {
			z := newBind(t, Options{ListenTCP: freePort(t, "tcp")})
			a := newBind(t, Options{})
			tap := &handshakeTap{Bind: a, drop: mode != "already-throttled"}
			if mode == "local-backpressure" {
				tap.failure = ErrBackpressure
			}
			ad, zd := wg(t, tap, "10.99.0.1"), wg(t, z, "10.99.0.2")
			if err := a.RegisterPeer(PeerIdentity{ID: "z", DataKey: key32(zd.public), ControlKey: [32]byte{1}}); err != nil {
				t.Fatal(err)
			}
			if err := z.RegisterPeer(PeerIdentity{ID: "a", DataKey: key32(ad.public), ControlKey: [32]byte{2}}); err != nil {
				t.Fatal(err)
			}
			ae, _ := a.PeerEndpoint("z")
			ze, _ := z.PeerEndpoint("a")
			for _, cfg := range []struct {
				from, to *testWG
				endpoint string
			}{{ad, zd, ae}, {zd, ad, ze}} {
				if err := cfg.from.dev.IpcSet(fmt.Sprintf("public_key=%s\nallowed_ip=%s/32\nendpoint=%s\n", cfg.to.public, cfg.to.ip, cfg.endpoint)); err != nil {
					t.Fatal(err)
				}
			}
			if err := zd.dev.Up(); err != nil {
				t.Fatal(err)
			}
			if err := ad.dev.Up(); err != nil {
				t.Fatal(err)
			}
			ctx, cancel := context.WithTimeout(context.Background(), time.Second)
			defer cancel()
			ref, err := a.Prepare(ctx, "z", fmt.Sprintf("tcp://127.0.0.1:%d", z.opt.ListenTCP))
			if err != nil {
				t.Fatal(err)
			}
			ep, err := a.PathEndpoint(ref)
			if err != nil {
				t.Fatal(err)
			}
			peer := ad.dev.LookupPeer(device.NoisePublicKey(key32(zd.public)))
			if mode == "already-throttled" {
				if err := peer.SendHandshakeInitiation(false); !errors.Is(err, net.ErrClosed) {
					t.Fatalf("default handshake before commit: %v", err)
				}
			}
			start := time.Now()
			if err := peer.SendHandshakeInitiationTo(false, ep); !errors.Is(err, tap.failure) {
				t.Fatal(err)
			}
			waitUntil(t, 8*time.Second, "timer retry completes data handshake before commit", func() bool {
				return handshakeSeconds(t, ad.dev) > 0 && handshakeSeconds(t, zd.dev) > 0
			})
			tap.mu.Lock()
			attempts := append([]conn.Endpoint(nil), tap.attempts...)
			tap.mu.Unlock()
			wantAttempts := 2
			if mode == "already-throttled" {
				wantAttempts = 1
			}
			if len(attempts) != wantAttempts {
				t.Fatalf("explicit attempts=%d, want %d", len(attempts), wantAttempts)
			}
			for _, endpoint := range attempts {
				p, ok := endpoint.(*pathEndpoint)
				if !ok || p.ref != ref {
					t.Fatal("timer retry lost exact path generation")
				}
			}
			logical, err := a.ParseEndpoint(ae)
			if err != nil {
				t.Fatal(err)
			}
			if err := a.Send([][]byte{{4, 0, 0, 0}}, logical); !errors.Is(err, net.ErrClosed) {
				t.Fatalf("uncommitted business used handshake path: %v", err)
			}
			a.mu.Lock()
			active := a.peers["z"].active
			a.mu.Unlock()
			if active.ID != "" {
				t.Fatal("retry committed business path")
			}
			t.Logf("%s: handshake confirmed after %s without another explicit call", mode, time.Since(start))
		})
	}
}
