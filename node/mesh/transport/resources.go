package transport

import (
	"errors"
	"golang.zx2c4.com/wireguard/conn"
)

func (b *Bind) SetResourceProfile(profile string) error {
	if profile != "member" && profile != "relay" && profile != "" {
		return errors.New("invalid resource profile")
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	b.opt.ResourceProfile = profile
	return nil
}

func (b *Bind) ValidatePeerSet(peers []PeerIdentity) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	if len(peers) > b.opt.limits().paths {
		return ErrBackpressure
	}
	ids, keys := map[string]bool{}, map[[32]byte]bool{}
	for _, p := range peers {
		if p.ID == "" || len(p.ID) > 256 || ids[p.ID] || p.DataKey == ([32]byte{}) || p.ControlKey == ([32]byte{}) || p.DataKey == p.ControlKey || keys[p.DataKey] || keys[p.ControlKey] {
			return errors.New("invalid or duplicate peer identity")
		}
		ids[p.ID], keys[p.DataKey], keys[p.ControlKey] = true, true, true
	}
	return nil
}

func (b *Bind) DeviceLimits() conn.DeviceLimits {
	return b.deviceLimits(0)
}

func (p *PlaneBind) DeviceLimits() conn.DeviceLimits {
	return p.b.deviceLimits(1)
}

// smallMessageBuffer holds every data message of a network MTU up to
// receiveMTU.
const smallMessageBuffer = 2048

// largeMessageBuffer holds a data message of a packet of wire.LargeMTU
// bytes, padded and sealed: the size class Go allocates for it anyway.
const largeMessageBuffer = 9472

// LargeSpares bounds each of the two lists of spare large buffers, of
// largeMessageBuffer bytes: the bind's, of the receive buffers WireGuard
// gave back for frames it took over (spares.go), and the TUN's in package
// mesh, of the send buffers it gave back for packets it took over. Each
// holds as many as the large queue it serves; with the large pools both stay
// within the budget TestTwoPlaneFixedBufferBudget checks.
const LargeSpares = 64

// spareFrameMin: a data frame longer than this is read into a large buffer.
// Go allocates such a frame in the size class of largeMessageBuffer anyway,
// so a spare one holds no more memory than the frame's own would, and the
// receive limits, which count frames by length, still bound what they hold.
// Shorter frames keep an allocation of their own size.
const spareFrameMin = 8192

func (b *Bind) deviceLimits(plane uint8) conn.DeviceLimits {
	b.mu.Lock()
	defer b.mu.Unlock()
	l := conn.DeviceLimits{MaxPeers: b.opt.limits().paths, Workers: 2, BatchSize: 8, QueueDepth: 4, TxBuffers: 32, RxBuffers: 64, KeepaliveBuffers: 4}
	if plane == 0 && b.opt.MTU <= receiveMTU {
		// Every data message of a network MTU up to receiveMTU fits 2048
		// bytes: six to seven times as many buffers as the full-size ones
		// in a fifth of their memory, for batches of 32, with packets
		// waiting for a session held to about the share they had.
		l.BatchSize, l.TxBuffers, l.RxBuffers, l.MessageBufferSize, l.PendingLimit = 32, 224, 384, smallMessageBuffer, 12
	}
	if plane == 0 && b.opt.largeFrames() {
		// Large packets get a pool of their own, apart from the 2048-byte
		// buffers, read and received in batches of 8. Receiving holds about
		// 900 KB of them in flight: a stream past 1 Gbit/s stalls on fewer.
		l.LargeBufferSize, l.LargeBatchSize, l.LargeTxBuffers, l.LargeRxBuffers = largeMessageBuffer, 8, 64, 96
	}
	if plane == 0 {
		l.PriorityRxBuffers = l.BatchSize + 1
	}
	if plane == 1 {
		l.Workers, l.TxBuffers, l.RxBuffers, l.BatchSize = 1, 8, 16, 1
	}
	if b.opt.ResourceProfile == "relay" {
		if l.PendingLimit > 0 {
			l.PendingLimit = 28
		}
		l.Workers *= 2
		l.TxBuffers *= 2
		l.RxBuffers *= 2
		l.LargeTxBuffers *= 2
		l.LargeRxBuffers *= 2
	}
	return l
}
