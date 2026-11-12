//go:build linux

package transport

import (
	"encoding/binary"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"time"
	"unsafe"

	"golang.org/x/net/ipv6"
	"golang.org/x/sys/unix"
	"golang.zx2c4.com/wireguard/conn"
	"openbear/node/mesh/clock"
)

// udpReadBatch is how many datagrams one read takes from the socket, of
// those already there; udpSendBatch the most one send takes.
const (
	udpReadBatch = 8
	udpSendBatch = 32
)

type udpBatchDatagram struct {
	data []byte
	addr netip.AddrPort
}

type udpBatchReader struct {
	pc      *ipv6.PacketConn
	msgs    []ipv6.Message
	packets []udpBatchDatagram
}

func newUDPBatchReader(u *udpSocket) *udpBatchReader {
	r := &udpBatchReader{pc: ipv6.NewPacketConn(u.UDPConn), msgs: make([]ipv6.Message, udpReadBatch), packets: make([]udpBatchDatagram, 0, udpReadBatch)}
	for i := range r.msgs {
		r.msgs[i].Buffers = [][]byte{make([]byte, 65540)}
	}
	return r
}

// read returns the datagrams of one read, valid until the next.
func (r *udpBatchReader) read() ([]udpBatchDatagram, error) {
	n, err := r.pc.ReadBatch(r.msgs, 0)
	if err != nil {
		return nil, err
	}
	r.packets = r.packets[:0]
	for i := 0; i < n; i++ {
		m := &r.msgs[i]
		a, ok := m.Addr.(*net.UDPAddr)
		if !ok || m.N < 0 || m.N > len(m.Buffers[0]) {
			continue
		}
		if ap := a.AddrPort(); ap.IsValid() {
			r.packets = append(r.packets, udpBatchDatagram{m.Buffers[0][:m.N], ap})
		}
	}
	return r.packets, nil
}

// writeBatch sends msgs, going on after a short count; it says how many
// went out and never sends one twice.
func writeBatch(msgs []ipv6.Message, write func([]ipv6.Message) (int, error)) (int, error) {
	sent := 0
	for sent < len(msgs) {
		n, err := write(msgs[sent:])
		sent += n
		if err != nil {
			return sent, err
		}
		if n == 0 {
			return sent, ErrBackpressure
		}
	}
	return sent, nil
}

// gsoControl is the UDP_SEGMENT ancillary data for segments of size bytes.
func gsoControl(size int) []byte {
	oob := make([]byte, unix.CmsgSpace(2))
	c := (*unix.Cmsghdr)(unsafe.Pointer(&oob[0]))
	c.Level = unix.SOL_UDP
	c.Type = unix.UDP_SEGMENT
	c.SetLen(unix.CmsgLen(2))
	binary.NativeEndian.PutUint16(oob[unix.CmsgLen(0):], uint16(size))
	return oob
}

type gsoWriter func(b, oob []byte, to netip.AddrPort) (n, oobn int, err error)

// gsoSend sends bufs, framed in head, as one datagram the kernel cuts into
// one per buf: every one but the last as long as the first, the last no
// longer. fallback: the socket or kernel refused the call before sending
// anything, and the batch may go another way. Any other failure is not
// sent again, as part of it may have gone out.
func gsoSend(bufs [][]byte, head [4]byte, to netip.AddrPort, write gsoWriter) (fallback bool, err error) {
	seg := len(bufs[0]) + 5
	data := make([]byte, 0, seg*len(bufs))
	for _, p := range bufs {
		data = append(append(append(data, head[:]...), 0), p...)
	}
	control := gsoControl(seg)
	n, oobn, err := write(data, control, to)
	if err == nil && n == len(data) && oobn == len(control) {
		return false, nil
	}
	if n == 0 && oobn == 0 && (errors.Is(err, unix.ENOPROTOOPT) || errors.Is(err, unix.EOPNOTSUPP) || errors.Is(err, unix.EINVAL) || errors.Is(err, unix.EMSGSIZE) || errors.Is(err, unix.EIO)) {
		return true, err
	}
	if err == nil {
		err = errors.New("short UDP write")
	}
	return false, fmt.Errorf("UDP segmentation: %d of %d bytes sent: %w", n, len(data), err)
}

// gsoFits: the batch is one segmentation can take.
func gsoFits(bufs [][]byte) bool {
	first := len(bufs[0])
	total := 0
	for i, p := range bufs {
		if len(p) > first || (len(p) < first && i != len(bufs)-1) {
			return false
		}
		total += len(p) + 5
	}
	return total <= 65507
}

// gsoRetry is how long a path sends without segmentation after the socket
// refused it, and after a send of it failed otherwise.
const (
	gsoRetryUnsupported = time.Minute
	gsoRetryFailed      = time.Second
)

// sendUDPBatch sends a batch of data datagrams to a UDP path in as few calls
// as it can: one segmented datagram, else one sendmmsg; false: it is not
// one this can take, and each datagram goes as before.
func (b *Bind) sendUDPBatch(bufs [][]byte, target conn.Endpoint, explicit bool, timing *sendTiming, deadline time.Time) (bool, error) {
	return b.sendUDPBatchWith(bufs, target, explicit, timing, deadline, nil)
}

// write, when not nil, stands in for the socket's segmented write.
func (b *Bind) sendUDPBatchWith(bufs [][]byte, target conn.Endpoint, explicit bool, timing *sendTiming, deadline time.Time, write gsoWriter) (bool, error) {
	if len(bufs) < 2 || len(bufs) > udpSendBatch {
		return false, nil
	}
	b.mu.Lock()
	r := b.run
	ep, err := b.resolve(target, 0, explicit)
	if err != nil || r == nil || ep.retired || ep.transport != "udp" || r.udp == nil || ep.udpWriters[0] >= udpWriters {
		b.mu.Unlock()
		return false, nil
	}
	limit := maxDatagram(b.opt.MTU)
	total := 0
	for _, p := range bufs {
		if len(p) == 0 || len(p) > limit {
			b.mu.Unlock()
			return false, nil
		}
		total += len(p) + 5
	}
	ep.udpWriters[0]++
	ep.stats.TxBufferedBytes += uint64(total)
	u, remote, head := r.udp, ep.remote, preambleFor(b.datagramVersion(ep.peer, ep.rxVersion))
	if ep.gsoSocket != u {
		ep.gsoSocket, ep.gsoRetryAfter = u, time.Time{}
	}
	segment := gsoFits(bufs) && !time.Now().Before(ep.gsoRetryAfter)
	b.mu.Unlock()
	to := netip.AddrPortFrom(remote.Addr().Unmap(), remote.Port())
	if timing != nil {
		timing.start = clock.Now()
	}
	sent := 0
	if segment {
		if write == nil {
			write = u.UDPConn.WriteMsgUDPAddrPort
		}
		var fallback bool
		fallback, err = gsoSend(bufs, head, to, write)
		retry := time.Duration(0)
		switch {
		case fallback:
			err, retry = nil, gsoRetryUnsupported
		case err != nil:
			retry = gsoRetryFailed
		default:
			sent = len(bufs)
		}
		segment = !fallback
		if retry != 0 {
			b.mu.Lock()
			if ep.gsoSocket == u {
				ep.gsoRetryAfter = time.Now().Add(retry)
			}
			b.mu.Unlock()
		}
	}
	if !segment {
		msgs := make([]ipv6.Message, len(bufs))
		heads := make([][5]byte, len(bufs))
		dest := net.UDPAddrFromAddrPort(to)
		for i, p := range bufs {
			copy(heads[i][:4], head[:])
			msgs[i] = ipv6.Message{Buffers: [][]byte{heads[i][:], p}, Addr: dest}
		}
		pc := ipv6.NewPacketConn(u.UDPConn)
		sent, err = writeBatch(msgs, func(m []ipv6.Message) (int, error) { return pc.WriteBatch(m, 0) })
	}
	if timing != nil {
		timing.end = clock.Now()
	}
	b.mu.Lock()
	ep.udpWriters[0]--
	ep.stats.TxBufferedBytes -= uint64(total)
	for _, p := range bufs[:sent] {
		ep.stats.TxBytes += uint64(len(p))
	}
	if sent > 0 {
		ep.stats.LocalAddr = u.local
	}
	b.mu.Unlock()
	if !segment && sent < len(bufs) {
		// What sendmmsg did not take goes one by one, as before.
		for _, p := range bufs[sent:] {
			if e := b.sendDatagram(p, target, 0, explicit, nil, deadline); err == nil {
				err = e
			}
		}
	}
	return true, err
}
