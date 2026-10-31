package node

import (
	"context"
	"crypto/rand"
	"encoding/binary"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"time"
)

// RFC 5389 Binding requests, the part of STUN that tells a client the
// address and port its packets arrive from.
const (
	stunCookie         = 0x2112a442
	stunBindingRequest = 0x0001
	stunBindingSuccess = 0x0101
	stunBindingError   = 0x0111
	stunMappedAddress  = 0x0001
	stunErrorCode      = 0x0009
	stunXORMapped      = 0x0020
	// Servers written against the drafts before RFC 5389 use this type.
	stunXORMappedOld = 0x8020
)

// stunRetries are the waits after each send; the request is sent again when
// one passes without an answer (RFC 5389 7.2.1, shortened).
var stunRetries = []time.Duration{500 * time.Millisecond, time.Second, 2 * time.Second}

// errSTUNForeign is a packet that is not the answer to our request.
var errSTUNForeign = errors.New("not our STUN transaction")

func stunRequest(txid [12]byte) []byte {
	msg := make([]byte, 20)
	binary.BigEndian.PutUint16(msg[0:], stunBindingRequest)
	binary.BigEndian.PutUint32(msg[4:], stunCookie)
	copy(msg[8:], txid[:])
	return msg
}

// stunParse reads the mapped address from the answer to txid,
// XOR-MAPPED-ADDRESS before MAPPED-ADDRESS.
func stunParse(msg []byte, txid [12]byte) (netip.AddrPort, error) {
	if len(msg) < 20 || msg[0]&0xc0 != 0 || binary.BigEndian.Uint32(msg[4:]) != stunCookie || [12]byte(msg[8:20]) != txid {
		return netip.AddrPort{}, errSTUNForeign
	}
	kind, size := binary.BigEndian.Uint16(msg[0:]), int(binary.BigEndian.Uint16(msg[2:]))
	if size%4 != 0 || 20+size > len(msg) {
		return netip.AddrPort{}, ddnsFail("parse_error", "malformed STUN message")
	}
	attrs := msg[20 : 20+size]
	var xored, mapped netip.AddrPort
	code := 0
	for len(attrs) >= 4 {
		typ, n := binary.BigEndian.Uint16(attrs[0:]), int(binary.BigEndian.Uint16(attrs[2:]))
		if 4+n > len(attrs) {
			return netip.AddrPort{}, ddnsFail("parse_error", "malformed STUN attribute")
		}
		value := attrs[4 : 4+n]
		switch typ {
		case stunXORMapped, stunXORMappedOld:
			if a, ok := stunAddress(value, txid, true); ok && !xored.IsValid() {
				xored = a
			}
		case stunMappedAddress:
			if a, ok := stunAddress(value, txid, false); ok && !mapped.IsValid() {
				mapped = a
			}
		case stunErrorCode:
			if n >= 4 {
				code = int(value[2]&0x07)*100 + int(value[3])
			}
		}
		attrs = attrs[min(4+(n+3)&^3, len(attrs)):]
	}
	switch kind {
	case stunBindingSuccess:
	case stunBindingError:
		return netip.AddrPort{}, ddnsFail("http_error", fmt.Sprintf("STUN error %d", code))
	default:
		return netip.AddrPort{}, errSTUNForeign
	}
	if xored.IsValid() {
		return xored, nil
	}
	if mapped.IsValid() {
		return mapped, nil
	}
	return netip.AddrPort{}, ddnsFail("parse_error", "no mapped address in the STUN answer")
}

func stunAddress(v []byte, txid [12]byte, xor bool) (netip.AddrPort, bool) {
	if len(v) < 4 {
		return netip.AddrPort{}, false
	}
	port := binary.BigEndian.Uint16(v[2:])
	var key [16]byte
	binary.BigEndian.PutUint32(key[:], stunCookie)
	copy(key[4:], txid[:])
	if xor {
		port ^= stunCookie >> 16
	}
	var addr netip.Addr
	switch {
	case v[1] == 1 && len(v) >= 8:
		var ip [4]byte
		for i := range ip {
			ip[i] = v[4+i]
			if xor {
				ip[i] ^= key[i]
			}
		}
		addr = netip.AddrFrom4(ip)
	case v[1] == 2 && len(v) >= 20:
		var ip [16]byte
		for i := range ip {
			ip[i] = v[4+i]
			if xor {
				ip[i] ^= key[i]
			}
		}
		addr = netip.AddrFrom16(ip)
	default:
		return netip.AddrPort{}, false
	}
	return netip.AddrPortFrom(addr, port), true
}

// ddnsSTUN asks each server in turn until one tells the mapped address.
func ddnsSTUN(ctx context.Context, servers []string, v6 bool, bindIface string) (netip.Addr, string, error) {
	d, err := ddnsDialer(bindIface, v6, true)
	if err != nil {
		return netip.Addr{}, "", err
	}
	network := "udp4"
	if v6 {
		network = "udp6"
	}
	var last *ddnsError
	for _, server := range servers {
		mapped, err := stunQuery(ctx, d, network, server)
		if err == nil {
			addr := mapped.Addr()
			if ddnsUsable(addr, v6) {
				return addr, server, nil
			}
			err = ddnsFail("parse_error", "the STUN answer has no usable "+ddnsFamilyName(v6)+" address")
		}
		code, detail := ddnsCode(err)
		last = ddnsFail(code, ddnsClip(server+": "+code+" "+detail))
		if ctx.Err() != nil {
			break
		}
	}
	if last == nil {
		return netip.Addr{}, "", ddnsFail("no_address", "no STUN server")
	}
	return netip.Addr{}, "", last
}

func stunQuery(ctx context.Context, d *net.Dialer, network, server string) (netip.AddrPort, error) {
	conn, err := d.DialContext(ctx, network, server)
	if err != nil {
		return netip.AddrPort{}, ddnsNetError(err)
	}
	defer conn.Close()
	stop := context.AfterFunc(ctx, func() { conn.SetReadDeadline(time.Now()) })
	defer stop()
	var txid [12]byte
	rand.Read(txid[:])
	req := stunRequest(txid)
	buf := make([]byte, 1500)
	for _, wait := range stunRetries {
		if _, err := conn.Write(req); err != nil {
			return netip.AddrPort{}, ddnsNetError(err)
		}
		conn.SetReadDeadline(time.Now().Add(wait))
		for {
			n, err := conn.Read(buf)
			if ctx.Err() != nil {
				return netip.AddrPort{}, ddnsFail("timeout", "")
			}
			var ne net.Error
			if errors.As(err, &ne) && ne.Timeout() {
				break
			}
			if err != nil {
				return netip.AddrPort{}, ddnsNetError(err)
			}
			mapped, err := stunParse(buf[:n], txid)
			if errors.Is(err, errSTUNForeign) {
				continue
			}
			return mapped, err
		}
	}
	return netip.AddrPort{}, ddnsFail("timeout", "")
}
