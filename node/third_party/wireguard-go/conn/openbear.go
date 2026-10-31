package conn

// ReceiveMetadata is local provenance, never a field received from the network.
// MessageTransportType notifications follow AEAD and replay validation.
type ReceiveMetadata struct {
	PublicKey   [32]byte
	Endpoint    Endpoint
	MessageType uint32
}

type SendMetadata struct {
	Endpoint Endpoint
	ID       uint64
}

// ResourceBound is opt-in for mesh TUNs that accept the smaller read batch.
// Limits are fixed at device construction and apply across all its peers.
// HandshakeQueueDepth and StagedDepth, when set, size the device's handshake
// queue and each peer's staged queue apart from QueueDepth: a device with
// many peers that start sessions on demand needs room for handshakes, and
// few staged packets per peer.
type DeviceLimits struct {
	MaxPeers, Workers, BatchSize, QueueDepth int
	TxBuffers, RxBuffers, KeepaliveBuffers   int
	HandshakeQueueDepth, StagedDepth         int
}

type ResourceBound interface {
	DeviceLimits() DeviceLimits
}

type AuthenticatedReceiver interface {
	AuthenticatedReceive(ReceiveMetadata) bool
}

// SourceAuthorizer is consulted only for a packet whose source AllowedIPs
// routes to another peer, after AEAD and replay validation. It may accept
// transit traffic from a forwarding peer; it never rejects what AllowedIPs
// accepts.
type SourceAuthorizer interface {
	AuthorizeSource(publicKey [32]byte, src []byte) bool
}

type MetadataWriter interface {
	WriteWithMetadata(bufs [][]byte, offset int, metadata []ReceiveMetadata) (int, error)
}

type MetadataReader interface {
	ReadWithMetadata(bufs [][]byte, sizes []int, offset int, metadata []SendMetadata) (int, error)
}

type MetadataSender interface {
	SendWithMetadata(bufs [][]byte, publicKey [32]byte, metadata SendMetadata) error
}
