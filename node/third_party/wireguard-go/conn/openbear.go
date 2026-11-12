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
	// Peer, when set, is the public key of the peer the packet goes to in
	// place of the one AllowedIPs gives its destination. Only the TUN sets
	// it, for a packet of its own; a packet whose peer is gone is dropped.
	Peer [32]byte
}

// ResourceBound is opt-in for mesh TUNs that accept the smaller read batch.
// Limits are fixed at device construction and apply across all its peers.
// HandshakeQueueDepth and StagedDepth, when set, size the device's handshake
// queue and each peer's staged queue apart from QueueDepth: a device with
// many peers that start sessions on demand needs room for handshakes, and
// few staged packets per peer.
//
// MessageBufferSize, when set below the largest message, sizes the TX and RX
// buffers: a device whose MTU is known fits more of them in the same memory.
// PendingLimit, when set, caps the packets all peers may hold waiting for a
// session below the half of TxBuffers otherwise allowed. PriorityRxBuffers
// are taken out of RxBuffers for the receive function PriorityReceiver
// names, so that its handshakes still find a buffer while data holds all
// the others.
//
// LargeTxBuffers and LargeRxBuffers of LargeBufferSize are a second pool,
// apart from the others, for messages longer than those hold: the reader
// of a TUN's LargeReader takes its buffers from it, LargeBatchSize at a
// time, and so does the receive function a LargeReceiver names.
type DeviceLimits struct {
	MaxPeers, Workers, BatchSize, QueueDepth int
	TxBuffers, RxBuffers, KeepaliveBuffers   int
	HandshakeQueueDepth, StagedDepth         int
	MessageBufferSize, PendingLimit          int
	PriorityRxBuffers                        int
	LargeBufferSize, LargeBatchSize          int
	LargeTxBuffers, LargeRxBuffers           int
}

type ResourceBound interface {
	DeviceLimits() DeviceLimits
}

// PriorityReceiver names the receive function, by index, that delivers
// handshakes apart from data; -1 for none.
type PriorityReceiver interface {
	PriorityReceiveIndex() int
}

// LargeReceiver names the receive function, by index, that delivers the
// messages too long for the device's other buffers; -1 for none.
type LargeReceiver interface {
	LargeReceiveIndex() int
}

// LargeReader is a TUN that hands the packets too long for the device's
// other buffers to a second reader, with the large pool's buffers.
type LargeReader interface {
	ReadLarge(bufs [][]byte, sizes []int, offset int, metadata []SendMetadata) (int, error)
}

// LargeSwapReader may replace returned buffer slices with exclusively owned
// buffers of the same size. The caller adopts those slices into its existing
// pool elements before encryption or any error cleanup. A slice displaced
// this way is the reader's from then on: the caller keeps no reference to it.
type LargeSwapReader interface {
	ReadLargeSwap(bufs [][]byte, sizes []int, offset int, metadata []SendMetadata) (int, error)
}

// LargeSwapReceiver gives a variant of the receive function LargeReceiver
// names that may hand a message over in a buffer of its own instead of
// copying it: it replaces packets[i] with an exclusively owned slice of the
// same length and capacity, the message at its start, and keeps the slice it
// displaced, which the caller holds no other reference to. The caller adopts
// packets[i], for every i below the count returned, also with an error, into
// the pool buffer it read into, before it uses, queues or returns any of
// them. LargeSwapReceive is called after Open, for the functions Open
// returned; nil for none.
type LargeSwapReceiver interface {
	LargeSwapReceive() ReceiveFunc
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
