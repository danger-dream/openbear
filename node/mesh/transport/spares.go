package transport

// Spare large receive buffers.
//
// A data frame longer than spareFrameMin read from a TCP stream is read into
// a buffer of largeMessageBuffer bytes, the size class Go allocates such a
// frame in anyway: a spare one when there is one, else a new one. The large
// queue's receive function, for a device that adopts what it hands over
// (LargeSwapReceive), gives WireGuard the frame in that buffer and keeps the
// one WireGuard read into as a spare; otherwise it copies the frame, and the
// frame's buffer becomes a spare. So does the buffer of a frame dropped on
// the way. Spares beyond LargeSpares are left to the GC.
//
// A large buffer is in one place at a time: the spares, the frame a reader
// is reading, the queue, or WireGuard.

// LargeReceiveCounts counts what became of large receive buffers: Swapped
// frames WireGuard took over in the buffer they were read into, Copied
// frames of the large queue it took as a copy; Reused frames read into a
// spare buffer, Allocated ones into a new one for want of a spare; Discarded
// buffers left to the GC, the spares being full.
type LargeReceiveCounts struct{ Swapped, Copied, Reused, Allocated, Discarded uint64 }

func (b *Bind) LargeReceive() LargeReceiveCounts {
	c := &b.largeRx
	return LargeReceiveCounts{Swapped: c.swapped.Load(), Copied: c.copied.Load(), Reused: c.reused.Load(), Allocated: c.allocated.Load(), Discarded: c.discarded.Load()}
}

// pooledFrame: a data frame of n bytes from s is read into a large buffer.
// TLS streams, control and frames of spareFrameMin bytes or fewer keep
// their own allocation.
func pooledFrame(r *bindRun, s *session, plane uint8, n int) bool {
	return r.spares != nil && plane == 0 && s.ep.transport == "tcp" && n > spareFrameMin && n <= largeMessageBuffer
}

// largeBuffer is a large buffer for a frame: a spare, or a new one.
func (b *Bind) largeBuffer(r *bindRun) []byte {
	select {
	case buf := <-r.spares:
		b.largeRx.reused.Add(1)
		return buf
	default:
		b.largeRx.allocated.Add(1)
		return make([]byte, largeMessageBuffer)
	}
}

// putSpare keeps buf, which nothing else holds any more, for a later frame;
// one the spares have no room for, or not a large buffer, goes to the GC.
func (b *Bind) putSpare(r *bindRun, buf []byte) {
	if r.spares == nil || cap(buf) != largeMessageBuffer {
		return
	}
	select {
	case r.spares <- buf[:largeMessageBuffer]:
	default:
		b.largeRx.discarded.Add(1)
	}
}

// dropFrame gives back the large buffer of p, a frame the bind drops.
func (b *Bind) dropFrame(r *bindRun, p received) {
	if p.pooled {
		b.putSpare(r, p.data)
	}
}
