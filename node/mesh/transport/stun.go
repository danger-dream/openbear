package transport

import (
	"context"
	"crypto/rand"
	"encoding/binary"
	"errors"
	"net"
	"net/netip"
)

// A STUN binding request (RFC 5389) from the WireGuard socket. Responses
// are taken only from the server asked and for a transaction this node
// started; the mesh's own datagrams begin with 'O' and never look like STUN.
const (
	stunCookie   = 0x2112a442
	stunRequest  = 0x0001
	stunResponse = 0x0101
	stunMapped   = 0x0001
	stunXOR      = 0x0020
)

type stunWaiter struct {
	server netip.AddrPort
	reply  chan netip.AddrPort
}

func isSTUN(p []byte) bool {
	return len(p) >= 20 && len(p)%4 == 0 && p[0]&0xc0 == 0 && binary.BigEndian.Uint32(p[4:8]) == stunCookie && int(binary.BigEndian.Uint16(p[2:4]))+20 == len(p)
}

// stunReply takes a response to one of this node's requests and reports
// whether p was one.
func (b *Bind) stunReply(p []byte, from netip.AddrPort) bool {
	if !isSTUN(p) {
		return false
	}
	var tx [12]byte
	copy(tx[:], p[8:20])
	b.mu.Lock()
	w := b.stunWait[tx]
	if w == nil || w.server != from {
		b.mu.Unlock()
		return false
	}
	addr, err := parseSTUN(p)
	if err != nil {
		b.mu.Unlock()
		return true // Invalid replies do not consume the pending transaction.
	}
	delete(b.stunWait, tx)
	b.mu.Unlock()
	w.reply <- addr
	return true
}

// parseSTUN reads the mapped address of a binding success response.
func parseSTUN(p []byte) (netip.AddrPort, error) {
	if !isSTUN(p) || binary.BigEndian.Uint16(p[0:2]) != stunResponse {
		return netip.AddrPort{}, errors.New("not a binding success response")
	}
	var mapped, xorMapped netip.AddrPort
	for a := p[20:]; len(a) > 0; {
		if len(a) < 4 {
			return netip.AddrPort{}, errors.New("truncated STUN attribute")
		}
		t, n := binary.BigEndian.Uint16(a[0:2]), int(binary.BigEndian.Uint16(a[2:4]))
		span := 4 + ((n + 3) &^ 3)
		if span > len(a) {
			return netip.AddrPort{}, errors.New("truncated STUN attribute padding")
		}
		v := a[4 : 4+n]
		if (t == stunXOR || t == stunMapped) && n == 8 && v[1] == 1 {
			port := binary.BigEndian.Uint16(v[2:4])
			ip := [4]byte(v[4:8])
			if t == stunXOR {
				port ^= stunCookie >> 16
				binary.BigEndian.PutUint32(ip[:], binary.BigEndian.Uint32(ip[:])^stunCookie)
			}
			got := netip.AddrPortFrom(netip.AddrFrom4(ip), port)
			if t == stunXOR {
				xorMapped = got
			} else {
				mapped = got
			}
		}
		a = a[span:]
	}
	if xorMapped.IsValid() {
		return xorMapped, nil
	}
	if mapped.IsValid() {
		return mapped, nil
	}
	return netip.AddrPort{}, errors.New("no IPv4 mapped address")
}

// STUN asks a STUN server which address the WireGuard socket is seen from.
func (b *Bind) STUN(ctx context.Context, server netip.AddrPort) (netip.AddrPort, error) {
	server = netip.AddrPortFrom(server.Addr().Unmap(), server.Port())
	req := make([]byte, 20)
	binary.BigEndian.PutUint16(req[0:2], stunRequest)
	binary.BigEndian.PutUint32(req[4:8], stunCookie)
	rand.Read(req[8:20])
	tx := [12]byte(req[8:20])
	w := &stunWaiter{server: server, reply: make(chan netip.AddrPort, 1)}
	b.mu.Lock()
	u := b.udp()
	if u == nil || len(b.stunWait) >= 16 {
		b.mu.Unlock()
		return netip.AddrPort{}, net.ErrClosed
	}
	b.stunWait[tx] = w
	b.mu.Unlock()
	defer func() {
		b.mu.Lock()
		delete(b.stunWait, tx)
		b.mu.Unlock()
	}()
	if _, err := u.WriteToUDPAddrPort(req, server); err != nil {
		return netip.AddrPort{}, err
	}
	select {
	case a := <-w.reply:
		if !a.IsValid() {
			return a, errors.New("bad STUN response")
		}
		return a, nil
	case <-ctx.Done():
		return netip.AddrPort{}, ctx.Err()
	}
}
