package device

import (
	"golang.zx2c4.com/wireguard/conn"
)

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
