//go:build !linux

package transport

import (
	"net/netip"
	"time"

	"golang.zx2c4.com/wireguard/conn"
)

type udpBatchDatagram struct {
	data []byte
	addr netip.AddrPort
}

// udpBatchReader reads one datagram at a time where there is no recvmmsg.
type udpBatchReader struct {
	u       *udpSocket
	buf     []byte
	packets [1]udpBatchDatagram
}

func newUDPBatchReader(u *udpSocket) *udpBatchReader {
	return &udpBatchReader{u: u, buf: make([]byte, 65540)}
}

func (r *udpBatchReader) read() ([]udpBatchDatagram, error) {
	n, a, err := r.u.ReadFromUDPAddrPort(r.buf)
	if err != nil {
		return nil, err
	}
	r.packets[0] = udpBatchDatagram{r.buf[:n], a}
	return r.packets[:], nil
}

func (b *Bind) sendUDPBatch([][]byte, conn.Endpoint, bool, *sendTiming, time.Time) (bool, error) {
	return false, nil
}
