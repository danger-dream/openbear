package device

import (
	"encoding/hex"
	"fmt"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"golang.zx2c4.com/wireguard/conn"
	"golang.zx2c4.com/wireguard/conn/bindtest"
)

func firstPeer(t *testing.T, dev *Device) *Peer {
	t.Helper()
	dev.peers.RLock()
	defer dev.peers.RUnlock()
	for _, p := range dev.peers.keyMap {
		return p
	}
	t.Fatal("missing peer")
	return nil
}

func TestHandshakeEndpointLifecycle(t *testing.T) {
	for _, action := range []string{"complete", "stop", "zero", "expire", "exhausted"} {
		t.Run(action, func(t *testing.T) {
			pair := genTestPair(t, false)
			peer := firstPeer(t, pair[0].dev)
			var ep conn.Endpoint = bindtest.ChannelEndpoint(12345)
			peer.endpoint.Lock()
			original := peer.endpoint.val
			peer.endpoint.Unlock()
			// A handshake in flight: an explicit call during its throttle redirects its retries.
			peer.handshake.mutex.Lock()
			peer.handshake.lastSentHandshake = time.Now()
			peer.handshake.mutex.Unlock()
			peer.timersHandshakeInitiated()
			if err := peer.SendHandshakeInitiationTo(false, ep); err != nil {
				t.Fatal(err)
			}
			if err := peer.SendHandshakeInitiation(true); err != nil {
				t.Fatal(err)
			}
			peer.handshake.mutex.RLock()
			pending := peer.handshakeEndpoint
			peer.handshake.mutex.RUnlock()
			if pending != ep {
				t.Fatal("throttled explicit endpoint lost to retry")
			}
			peer.endpoint.Lock()
			selected := peer.endpoint.val
			peer.endpoint.Unlock()
			if selected != original {
				t.Fatal("handshake changed peer endpoint")
			}
			switch action {
			case "complete":
				peer.timersHandshakeComplete()
			case "stop":
				peer.Stop()
			case "zero":
				peer.ZeroAndFlushAll()
			case "expire":
				peer.ExpireCurrentKeypairs()
			case "exhausted":
				peer.timers.handshakeAttempts.Store(MaxTimerHandshakes + 1)
				expiredRetransmitHandshake(peer)
			}
			peer.handshake.mutex.RLock()
			pending = peer.handshakeEndpoint
			peer.handshake.mutex.RUnlock()
			if pending != nil {
				t.Fatal("handshake endpoint retained after", action)
			}
		})
	}
}

func TestThrottledEndpointWithNoHandshakeInFlightIsNotKept(t *testing.T) {
	pair := genTestPair(t, false)
	peer := firstPeer(t, pair[0].dev)
	// The pair's handshake is done; a punch or warm-up arriving in the next
	// seconds must not become the target of a handshake minutes later.
	peer.handshake.mutex.Lock()
	peer.handshake.lastSentHandshake = time.Now()
	peer.handshake.mutex.Unlock()
	if err := peer.SendHandshakeInitiationTo(false, bindtest.ChannelEndpoint(9)); err != nil {
		t.Fatal(err)
	}
	peer.handshake.mutex.RLock()
	pending := peer.handshakeEndpoint
	peer.handshake.mutex.RUnlock()
	if pending != nil {
		t.Fatal("throttled endpoint kept with no handshake in flight")
	}
}

func TestKeptEndpointThatFailsGivesWayToThePeerEndpoint(t *testing.T) {
	pair := genTestPair(t, false)
	peer := firstPeer(t, pair[0].dev)
	// Kept from an earlier handshake and refused by the bind now, as a
	// released path or an ended punch is.
	gone := bindtest.ChannelEndpoint(9)
	peer.handshake.mutex.Lock()
	peer.handshakeEndpoint = gone
	peer.handshake.lastSentHandshake = time.Now().Add(-(RekeyTimeout + time.Second))
	peer.handshake.mutex.Unlock()
	before := peer.txBytes.Load()
	if err := peer.SendHandshakeInitiation(true); err != nil {
		t.Fatalf("retry through the peer endpoint: %v", err)
	}
	if sent := peer.txBytes.Load() - before; sent != MessageInitiationSize {
		t.Fatalf("sent %d bytes, want one initiation", sent)
	}
	peer.handshake.mutex.RLock()
	pending := peer.handshakeEndpoint
	peer.handshake.mutex.RUnlock()
	if pending != nil {
		t.Fatal("failed endpoint still kept")
	}
	// An explicit target is the caller's: its failure is returned, not rerouted.
	peer.handshake.mutex.Lock()
	peer.handshake.lastSentHandshake = time.Now().Add(-(RekeyTimeout + time.Second))
	peer.handshake.mutex.Unlock()
	if err := peer.SendHandshakeInitiationTo(false, gone); err == nil {
		t.Fatal("explicit failure hidden")
	}
}

// TestStatsMatchIpcGet: Stats gives what IpcGet writes, peer by peer: after
// a handshake and traffic, with a keepalive and several allowed IPs.
func TestStatsMatchIpcGet(t *testing.T) {
	pair := genTestPair(t, false)
	pair.Send(t, Ping, nil)
	pair.Send(t, Pong, nil)
	peer := firstPeer(t, pair[0].dev)
	key := peer.handshake.remoteStatic
	if err := pair[0].dev.IpcSet(fmt.Sprintf("public_key=%s\npersistent_keepalive_interval=25\nallowed_ip=10.9.0.0/16\nallowed_ip=fd00::/64\n", hex.EncodeToString(key[:]))); err != nil {
		t.Fatal(err)
	}
	for _, dev := range []*Device{pair[0].dev, pair[1].dev} {
		text, err := dev.IpcGet()
		if err != nil {
			t.Fatal(err)
		}
		want := map[string]string{}
		port, cur, sec := "", "", int64(0)
		for _, line := range strings.Split(text, "\n") {
			k, v, _ := strings.Cut(line, "=")
			switch k {
			case "listen_port":
				port = v
			case "public_key":
				cur = v
			case "endpoint", "tx_bytes", "rx_bytes", "persistent_keepalive_interval":
				want[cur] += k + "=" + v + " "
			case "last_handshake_time_sec":
				sec, _ = strconv.ParseInt(v, 10, 64)
			case "last_handshake_time_nsec":
				n, _ := strconv.ParseInt(v, 10, 64)
				want[cur] += fmt.Sprintf("handshake=%d ", sec*1e9+n)
			case "allowed_ip":
				want[cur] += "allowed=" + v + " "
			}
		}
		listen, stats := dev.Stats()
		if fmt.Sprint(listen) != port || len(stats) != len(want) {
			t.Fatalf("port %d, %d peers; IpcGet: port %s, %d peers", listen, len(stats), port, len(want))
		}
		for _, s := range stats {
			got := ""
			if s.Endpoint != "" {
				got += "endpoint=" + s.Endpoint + " "
			}
			got += fmt.Sprintf("handshake=%d tx_bytes=%d rx_bytes=%d persistent_keepalive_interval=%d ", s.HandshakeNano, s.TxBytes, s.RxBytes, s.Keepalive)
			for _, p := range s.AllowedIPs {
				got += "allowed=" + p.String() + " "
			}
			if w := want[hex.EncodeToString(s.PublicKey[:])]; got != w {
				t.Fatalf("Stats: %s\nIpcGet: %s", got, w)
			}
			if p := dev.LookupPeer(s.PublicKey); p == nil || p.LastHandshakeNano() != s.HandshakeNano {
				t.Fatalf("LastHandshakeNano of %x is not %d", s.PublicKey, s.HandshakeNano)
			}
		}
	}
}

// TestCookieRetriesTheInitiationAtOnce: a responder under load answers the
// first initiation with a cookie; the initiator sends it again with mac2 at
// once, and its first packet goes through in a round trip or two, not after
// the 5 s retransmit timer that used to drop whatever waited for the session.
func TestCookieRetriesTheInitiationAtOnce(t *testing.T) {
	pair := genTestPair(t, false)
	// pair[1] sends the ping, so pair[0] answers the handshake.
	pair[0].dev.rate.underLoadUntil.Store(time.Now().Add(time.Hour).UnixNano())
	start := time.Now()
	pair.Send(t, Ping, nil)
	if took := time.Since(start); took > time.Second {
		t.Fatalf("first packet took %v under load, want well under the retransmit timer", took)
	}
	if got := pair[0].dev.HandshakeStats(); got.CookiesSent == 0 {
		t.Fatalf("responder stats %+v, want a cookie sent", got)
	}
	if got := pair[1].dev.HandshakeStats(); got.CookiesReceived == 0 || got.Retries != 0 {
		t.Fatalf("initiator stats %+v, want a cookie consumed and no timer retry", got)
	}
}

// TestRetryAfterCookieConcurrentSendsOnce: concurrent cookie replies across
// multiple handshake workers send at most one immediate cookie retry per
// in-flight initiation.
func TestRetryAfterCookieConcurrentSendsOnce(t *testing.T) {
	pair := genTestPair(t, false)
	// Stop the responder so the initiation stays in flight with its retransmit timer pending.
	pair[1].dev.Down()
	peer := firstPeer(t, pair[0].dev)
	peer.handshake.mutex.Lock()
	peer.handshake.lastSentHandshake = time.Now().Add(-(RekeyTimeout + time.Second))
	peer.handshake.mutex.Unlock()
	if err := peer.SendHandshakeInitiation(false); err != nil {
		t.Fatal(err)
	}
	before := peer.txBytes.Load()
	var wg sync.WaitGroup
	start := make(chan struct{})
	for range 8 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			peer.retryAfterCookie()
		}()
	}
	close(start)
	wg.Wait()
	if sent := peer.txBytes.Load() - before; sent != MessageInitiationSize {
		t.Fatalf("concurrent retryAfterCookie sent %d bytes (%d initiations), want 1 initiation (%d bytes)",
			sent, sent/MessageInitiationSize, MessageInitiationSize)
	}
}
