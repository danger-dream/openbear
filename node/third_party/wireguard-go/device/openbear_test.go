package device

import (
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
