package transport

import (
	"crypto/tls"
	"encoding/binary"
	"errors"
	"io"
	"net"
	"sync"

	"openbear/node/mesh/wire"
)

// preamble opens every stream and datagram: "OBM" and a digit, "2" for
// mesh protocol 1 (package wire), "3" for 2 and so on. A stream's says the
// newest protocol its sender speaks; a datagram's, the one it is in. This
// is the one of this build's protocol.
const preamble = "OBM" + string(rune('1'+wire.Protocol))

func preambleFor(version int) [4]byte { return [4]byte{'O', 'B', 'M', byte('1' + version)} }

// preambleVersion is the protocol a preamble names, 0 for none.
func preambleVersion(b []byte) int {
	if len(b) < 4 || string(b[:3]) != "OBM" || b[3] < '2' || b[3] > '9' {
		return 0
	}
	return int(b[3] - '1')
}

// knownPlane: 0 is WireGuard data, 1 control. A later protocol may add
// others; a frame or datagram of one is skipped, not an error.
func knownPlane(p uint8) bool { return p <= 1 }

var ErrFrame = errors.New("invalid mesh transport frame")

func maxDatagram(mtu int) int { return max(148, (mtu+15)&^15+32) }

// receiveMTU is the largest network MTU a mesh config can set. Members take
// a new MTU one at a time, and one offline or failing to apply keeps the
// old one, so what arrives is accepted up to that whatever this node's own
// MTU. On a stream an oversized frame would otherwise end the connection.
const receiveMTU = 1420

func receiveLimit(mtu int) int { return maxDatagram(max(mtu, receiveMTU)) }

// largeLimit is the longest data frame a stream of wire.LargeFrames or
// later carries between two nodes that take large frames: a packet of
// wire.LargeMTU bytes and what padding to a network MTU below it adds.
var largeLimit = maxDatagram(wire.LargeMTU + 15)

// largeFrames: the bind has the queue and the device the buffers for
// large frames, which only the 2048-byte buffers leave room for.
func (o Options) largeFrames() bool { return o.LargeFrames && o.MTU <= receiveMTU }

// streamReceiveLimit is the longest frame s takes. Called with b.mu held.
func (b *Bind) streamReceiveLimit(s *session) int {
	if b.opt.largeFrames() && s.version >= wire.LargeFrames {
		return largeLimit
	}
	return receiveLimit(b.opt.MTU)
}

// sendLimit is the longest data frame that goes out on ss, ep's stream,
// or on ep itself when it is UDP: a large one only over a stream of
// wire.LargeFrames or later whose other end has said it takes them
// (session.peerLarge). What the other end takes on a stream does not
// change while it lasts, so a frame sealed for it before the peer or this
// node stopped asking for large packets still goes. Called with b.mu held.
func (b *Bind) sendLimit(ep *endpoint, ss *session) int {
	if ep.transport != "udp" && ss != nil && ss.version >= wire.LargeFrames && ss.peerLarge && b.opt.largeFrames() {
		return largeLimit
	}
	return maxDatagram(b.opt.MTU)
}

func readPreamble(r io.Reader) error {
	_, err := readPreambleVersion(r)
	return err
}

// readPreambleVersion reads a stream's preamble and returns the newest
// protocol its sender speaks, also one newer than this node's.
func readPreambleVersion(r io.Reader) (int, error) {
	var b [4]byte
	if _, err := io.ReadFull(r, b[:]); err != nil {
		return 0, err
	}
	v := preambleVersion(b[:])
	if v == 0 {
		return 0, ErrFrame
	}
	return v, nil
}

func readFrame(r io.Reader, buf []byte) (int, error) {
	n, err := readFrameSize(r, len(buf))
	if err != nil {
		return 0, err
	}
	_, err = io.ReadFull(r, buf[:n])
	return n, err
}

func readFrameSize(r io.Reader, limit int) (int, error) {
	n, plane, err := readFrameHeader(r, limit)
	if err == nil && plane != 0 {
		err = ErrFrame
	}
	return n, err
}
func readFrameHeader(r io.Reader, limit int) (int, uint8, error) {
	var h [3]byte
	if _, err := io.ReadFull(r, h[:]); err != nil {
		return 0, 0, err
	}
	n := int(binary.BigEndian.Uint16(h[:2])) - 1
	if !knownPlane(h[2]) {
		// Its length still delimits it: the caller skips it.
		if n < 1 {
			return 0, 0, ErrFrame
		}
		return n, h[2], nil
	}
	if n < 1 || n > limit || (h[2] == 1 && n > maxDatagram(1044)) {
		return 0, 0, ErrFrame
	}
	return n, h[2], nil
}
func writeFrame(w io.Writer, p []byte, limit int) error { return writePlaneFrame(w, p, limit, 0) }
func writePlaneFrame(w io.Writer, p []byte, limit int, plane uint8) error {
	if plane > 1 || len(p) == 0 || len(p) > limit || len(p) > 65534 || (plane == 1 && len(p) > maxDatagram(1044)) {
		return ErrFrame
	}
	var h [3]byte
	binary.BigEndian.PutUint16(h[:2], uint16(len(p)+1))
	h[2] = plane
	switch c := w.(type) {
	case *net.TCPConn:
		return writeVector(c, net.Buffers{h[:], p}, len(h)+len(p))
	case *tls.Conn:
		// One record, not one for the header and one for the payload.
		buf := frameBuffers.Get().(*[]byte)
		frame := append(append((*buf)[:0], h[:]...), p...)
		err := writeAll(c, frame)
		*buf = frame
		frameBuffers.Put(buf)
		return err
	}
	if err := writeAll(w, h[:]); err != nil {
		return err
	}
	return writeAll(w, p)
}

// frameBuffers hold a frame laid out whole for a TLS write.
var frameBuffers = sync.Pool{New: func() any { return new([]byte) }}

// writeVector writes v, total bytes, in one vectored write. A short write
// is an error: the caller closes the stream, a prefix of a frame having
// gone out.
func writeVector(c *net.TCPConn, v net.Buffers, total int) error {
	n, err := v.WriteTo(c)
	if err == nil && n != int64(total) {
		err = io.ErrShortWrite
	}
	return err
}

func writeAll(w io.Writer, p []byte) error {
	for len(p) > 0 {
		n, err := w.Write(p)
		if err != nil {
			return err
		}
		if n <= 0 || n > len(p) {
			return io.ErrShortWrite
		}
		p = p[n:]
	}
	return nil
}

func unwrapUDP(p []byte, limit int) ([]byte, error) {
	payload, plane, err := unwrapPlaneUDP(p, limit)
	if err == nil && plane != 0 {
		err = ErrFrame
	}
	return payload, err
}
func unwrapPlaneUDP(p []byte, limit int) ([]byte, uint8, error) {
	payload, plane, v, err := unwrapDatagram(p, limit)
	if err == nil && (!knownPlane(plane) || !wire.Local().Has(v)) {
		err = ErrFrame
	}
	return payload, plane, err
}

// unwrapDatagram returns a datagram's payload, plane and protocol version.
// An unknown plane or version is the caller's to drop.
func unwrapDatagram(p []byte, limit int) ([]byte, uint8, int, error) {
	v := preambleVersion(p)
	if len(p) <= 5 || v == 0 {
		return nil, 0, 0, ErrFrame
	}
	if knownPlane(p[4]) && (len(p)-5 > limit || (p[4] == 1 && len(p)-5 > maxDatagram(1044))) {
		return nil, 0, 0, ErrFrame
	}
	return p[5:], p[4], v, nil
}
