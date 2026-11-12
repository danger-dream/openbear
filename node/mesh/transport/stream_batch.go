package transport

import (
	"crypto/tls"
	"encoding/binary"
	"io"
	"net"
	"time"

	"golang.zx2c4.com/wireguard/conn"
	"openbear/node/mesh/clock"
)

// streamBatchMax is the most data frames written in one call, the data
// device's batch.
const streamBatchMax = 32

// writeFrameBatch writes bufs as data frames in one call to write and says
// how many of them went out whole. On plain TCP write is one vectored write;
// on TLS it gets the frames laid out in one buffer, which TLS cuts into
// records of up to 16 KiB. Any error, a short write included, leaves the
// caller to close the stream.
func writeFrameBatch(bufs [][]byte, write func(net.Buffers) (int64, error)) (int, error) {
	var heads [streamBatchMax][3]byte
	var vec [2 * streamBatchMax][]byte
	var total int64
	for i, p := range bufs {
		binary.BigEndian.PutUint16(heads[i][:2], uint16(len(p)+1))
		vec[2*i] = heads[i][:]
		vec[2*i+1] = p
		total += int64(len(p) + 3)
	}
	n, err := write(net.Buffers(vec[:2*len(bufs)]))
	complete := 0
	for remaining := n; complete < len(bufs) && remaining >= int64(len(bufs[complete])+3); complete++ {
		remaining -= int64(len(bufs[complete]) + 3)
	}
	if err == nil && n != total {
		err = io.ErrShortWrite
	}
	return complete, err
}

func streamWriter(c net.Conn) func(net.Buffers) (int64, error) {
	if tc, ok := c.(*tls.Conn); ok {
		return func(v net.Buffers) (int64, error) {
			buf := frameBuffers.Get().(*[]byte)
			frame := (*buf)[:0]
			for _, b := range v {
				frame = append(frame, b...)
			}
			err := writeAll(tc, frame)
			n := int64(len(frame))
			if err != nil {
				n = 0
			}
			*buf = frame
			frameBuffers.Put(buf)
			return n, err
		}
	}
	return func(v net.Buffers) (int64, error) { return v.WriteTo(c) }
}

// sendStreamBatch writes a batch of data frames to a TCP or TLS path in one
// call; false: it is not one this can take, and each frame goes as before.
func (b *Bind) sendStreamBatch(bufs [][]byte, target conn.Endpoint, explicit bool, timing *sendTiming, deadline time.Time) (bool, error) {
	return b.sendStreamBatchWith(bufs, target, explicit, timing, deadline, nil)
}

// write, when not nil, stands in for the connection's.
func (b *Bind) sendStreamBatchWith(bufs [][]byte, target conn.Endpoint, explicit bool, timing *sendTiming, deadline time.Time, write func(net.Buffers) (int64, error)) (bool, error) {
	if len(bufs) < 2 || len(bufs) > streamBatchMax {
		return false, nil
	}
	b.mu.Lock()
	r := b.run
	ep, err := b.resolve(target, 0, explicit)
	if err != nil || r == nil || ep.retired || ep.transport == "udp" {
		b.mu.Unlock()
		return false, nil
	}
	ss := ep.current
	if ss == nil || ss.closed.Load() {
		b.mu.Unlock()
		return false, nil
	}
	limit := b.sendLimit(ep, ss)
	total := 0
	for _, p := range bufs {
		// A handshake goes over the companion, which the frame-by-frame
		// path picks.
		if len(p) == 0 || len(p) > limit || isHandshake(p) {
			b.mu.Unlock()
			return false, nil
		}
		total += len(p) + 3
	}
	if err := b.acquireSend(r, ss, 0, deadline); err != nil {
		b.mu.Unlock()
		return false, nil
	}
	// acquireSend may have let go of b.mu: the path may have moved on.
	current, err := b.resolve(target, 0, explicit)
	if err != nil || current != ep || ep.current != ss || ss.closed.Load() || b.run != r {
		ss.releaseSend()
		b.mu.Unlock()
		if err == nil {
			err = net.ErrClosed
		}
		return true, err
	}
	ep.stats.TxBufferedBytes = uint64(total)
	b.mu.Unlock()
	if timing != nil {
		timing.start = clock.Now()
	}
	if write == nil {
		write = streamWriter(ss.conn)
	}
	complete := 0
	err = ss.conn.SetWriteDeadline(deadline)
	if err == nil {
		complete, err = writeFrameBatch(bufs, write)
	}
	if timing != nil {
		timing.end = clock.Now()
	}
	if err != nil {
		ss.closed.Store(true)
		ss.conn.Close()
	}
	b.mu.Lock()
	if ep.current == ss {
		ep.stats.TxBufferedBytes = 0
	}
	for _, p := range bufs[:complete] {
		ep.stats.TxBytes += uint64(len(p))
	}
	ss.releaseSend()
	b.mu.Unlock()
	return true, err
}
