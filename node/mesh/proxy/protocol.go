package proxy

import (
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"net/netip"
	"strconv"
)

// The tunnel: an entry opens a TCP connection to the exit's mesh address,
// maybe long before it has a client for it, and when it does sends
//
//	version (1) | command | address type | address | port (2)
//
// then, without waiting, the client's data. The exit answers one status byte,
// the SOCKS5 reply codes, and then the target's data. A UDP association
// carries datagrams as frames both ways: length (2) | address | payload.
const tunnelVersion = 1

const (
	cmdConnect = 1
	cmdUDP     = 3
)

const (
	atypIPv4   = 1
	atypDomain = 3
	atypIPv6   = 4
)

// Status codes, those of SOCKS5 replies.
const (
	statusOK          = 0
	statusFailure     = 1
	statusNotAllowed  = 2
	statusNetwork     = 3
	statusHost        = 4
	statusRefused     = 5
	statusTimeout     = 6
	statusCommand     = 7
	statusAddressType = 8
)

// Addr is a target: a domain the exit resolves, or an address.
type Addr struct {
	Host string // a domain; "" when IP is set
	IP   netip.Addr
	Port uint16
}

func (a Addr) String() string {
	if a.Host != "" {
		return net.JoinHostPort(a.Host, strconv.Itoa(int(a.Port)))
	}
	return netip.AddrPortFrom(a.IP, a.Port).String()
}

// appendAddr writes a as SOCKS5 does: type, address, port.
func appendAddr(b []byte, a Addr) []byte {
	switch {
	case a.Host != "":
		b = append(b, atypDomain, byte(len(a.Host)))
		b = append(b, a.Host...)
	case a.IP.Is4():
		ip := a.IP.As4()
		b = append(append(b, atypIPv4), ip[:]...)
	default:
		ip := a.IP.As16()
		b = append(append(b, atypIPv6), ip[:]...)
	}
	return binary.BigEndian.AppendUint16(b, a.Port)
}

var errAddressType = errors.New("unsupported address type")

// readAddr reads what appendAddr wrote, exactly, so that nothing past it is
// buffered: the rest of the stream may be spliced.
func readAddr(r io.Reader) (Addr, error) {
	var t [1]byte
	if _, err := io.ReadFull(r, t[:]); err != nil {
		return Addr{}, err
	}
	var a Addr
	switch t[0] {
	case atypIPv4:
		var ip [4]byte
		if _, err := io.ReadFull(r, ip[:]); err != nil {
			return Addr{}, err
		}
		a.IP = netip.AddrFrom4(ip)
	case atypIPv6:
		var ip [16]byte
		if _, err := io.ReadFull(r, ip[:]); err != nil {
			return Addr{}, err
		}
		a.IP = netip.AddrFrom16(ip).Unmap()
	case atypDomain:
		var n [1]byte
		if _, err := io.ReadFull(r, n[:]); err != nil {
			return Addr{}, err
		}
		host := make([]byte, n[0])
		if _, err := io.ReadFull(r, host); err != nil {
			return Addr{}, err
		}
		if ip, err := netip.ParseAddr(string(host)); err == nil {
			a.IP = ip.Unmap()
		} else if n[0] == 0 {
			return Addr{}, errors.New("empty domain")
		} else {
			a.Host = string(host)
		}
	default:
		return Addr{}, errAddressType
	}
	var p [2]byte
	if _, err := io.ReadFull(r, p[:]); err != nil {
		return Addr{}, err
	}
	a.Port = binary.BigEndian.Uint16(p[:])
	return a, nil
}

// parseAddr reads an address from the front of b, returning the rest.
func parseAddr(b []byte) (Addr, []byte, error) {
	r := &sliceReader{b: b}
	a, err := readAddr(r)
	if err != nil {
		return Addr{}, nil, err
	}
	return a, b[r.n:], nil
}

type sliceReader struct {
	b []byte
	n int
}

func (s *sliceReader) Read(p []byte) (int, error) {
	if s.n >= len(s.b) {
		return 0, io.ErrUnexpectedEOF
	}
	n := copy(p, s.b[s.n:])
	s.n += n
	return n, nil
}

func tunnelHeader(cmd byte, a Addr) []byte {
	return appendAddr([]byte{tunnelVersion, cmd}, a)
}

// readHeader reads a tunnel's header.
func readHeader(r io.Reader) (byte, Addr, error) {
	var h [2]byte
	if _, err := io.ReadFull(r, h[:]); err != nil {
		return 0, Addr{}, err
	}
	if h[0] != tunnelVersion {
		return 0, Addr{}, fmt.Errorf("tunnel version %d", h[0])
	}
	a, err := readAddr(r)
	return h[1], a, err
}

// A UDP frame: length of the rest (2), the address, the payload.
func writeFrame(w io.Writer, a Addr, payload []byte) error {
	b := make([]byte, 2, 2+22+len(payload))
	b = append(appendAddr(b, a), payload...)
	if len(b)-2 > 0xffff {
		return errors.New("datagram too long")
	}
	binary.BigEndian.PutUint16(b, uint16(len(b)-2))
	_, err := w.Write(b)
	return err
}

func readFrame(r io.Reader, buf []byte) (Addr, []byte, error) {
	var n [2]byte
	if _, err := io.ReadFull(r, n[:]); err != nil {
		return Addr{}, nil, err
	}
	size := int(binary.BigEndian.Uint16(n[:]))
	if size > len(buf) {
		return Addr{}, nil, errors.New("frame too long")
	}
	if _, err := io.ReadFull(r, buf[:size]); err != nil {
		return Addr{}, nil, err
	}
	a, rest, err := parseAddr(buf[:size])
	return a, rest, err
}
