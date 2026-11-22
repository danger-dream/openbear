package device

import (
	"net/netip"

	"golang.zx2c4.com/wireguard/conn"
)

// PeerStats is what IpcGet reports of a peer but its private and preshared
// keys.
type PeerStats struct {
	PublicKey        NoisePublicKey
	Endpoint         string // as endpoint=, "" without one
	HandshakeNano    int64
	RxBytes, TxBytes uint64
	Keepalive        uint32
	AllowedIPs       []netip.Prefix
}

// Stats is the listen port and the peers as IpcGet reports them, without
// writing and parsing the text.
func (device *Device) Stats() (listenPort uint16, peers []PeerStats) {
	device.ipcMutex.RLock()
	defer device.ipcMutex.RUnlock()
	device.net.RLock()
	defer device.net.RUnlock()
	device.peers.RLock()
	defer device.peers.RUnlock()
	peers = make([]PeerStats, 0, len(device.peers.keyMap))
	for _, peer := range device.peers.keyMap {
		peer.handshake.mutex.RLock()
		s := PeerStats{PublicKey: peer.handshake.remoteStatic}
		peer.handshake.mutex.RUnlock()
		peer.endpoint.Lock()
		if peer.endpoint.val != nil {
			s.Endpoint = peer.endpoint.val.DstToString()
		}
		peer.endpoint.Unlock()
		s.HandshakeNano = peer.lastHandshakeNano.Load()
		s.TxBytes, s.RxBytes = peer.txBytes.Load(), peer.rxBytes.Load()
		s.Keepalive = peer.persistentKeepaliveInterval.Load()
		device.allowedips.EntriesForPeer(peer, func(prefix netip.Prefix) bool {
			s.AllowedIPs = append(s.AllowedIPs, prefix)
			return true
		})
		peers = append(peers, s)
	}
	return device.net.port, peers
}

// LastHandshakeNano is last_handshake_time of IpcGet in nanoseconds: when
// the last handshake completed, 0 before one.
func (peer *Peer) LastHandshakeNano() int64 { return peer.lastHandshakeNano.Load() }

// retryAfterCookie sends the initiation in flight again at once, now with
// the mac2 of the cookie just consumed, instead of on its retransmit timer:
// the responder, under load, answered it with the cookie and dropped it.
// Once per initiation, so that cookies, which anyone who sees the initiation
// can make, cannot have it sent over and over.
func (peer *Peer) retryAfterCookie() {
	_ = peer.sendHandshakeInitiation(true, nil, true)
}

func (peer *Peer) clearHandshakeEndpoint() {
	peer.handshake.mutex.Lock()
	peer.handshakeEndpoint = nil
	peer.handshake.mutex.Unlock()
}

func (device *Device) authenticated(peer *Peer, endpoint conn.Endpoint, messageType uint32) bool {
	if observer, ok := device.net.bind.(conn.AuthenticatedReceiver); ok {
		return observer.AuthenticatedReceive(conn.ReceiveMetadata{PublicKey: [32]byte(peer.handshake.remoteStatic), Endpoint: endpoint, MessageType: messageType})
	}
	return true
}

func (device *Device) sourceAllowed(peer *Peer, src []byte) bool {
	if device.allowedips.Lookup(src) == peer {
		return true
	}
	if a, ok := device.tun.device.(conn.SourceAuthorizer); ok {
		return a.AuthorizeSource([32]byte(peer.handshake.remoteStatic), src)
	}
	return false
}

func (peer *Peer) sendWithMetadata(buffers [][]byte, metadata conn.SendMetadata) error {
	if metadata.Endpoint == nil {
		return peer.SendBuffers(buffers)
	}
	peer.device.net.RLock()
	defer peer.device.net.RUnlock()
	if peer.device.isClosed() {
		return nil
	}
	var err error
	if sender, ok := peer.device.net.bind.(conn.MetadataSender); ok {
		err = sender.SendWithMetadata(buffers, [32]byte(peer.handshake.remoteStatic), metadata)
	} else {
		err = peer.device.net.bind.Send(buffers, metadata.Endpoint)
	}
	if err == nil {
		for _, b := range buffers {
			peer.txBytes.Add(uint64(len(b)))
		}
	}
	return err
}
