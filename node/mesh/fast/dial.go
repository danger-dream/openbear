package fast

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/binary"
	"errors"
	"io"
	"net"
	"net/netip"
	"time"
)

// dialPreamble is the key and the target a Dial writes first on its
// loopback connection; dialOpened the byte it waits for back, once the
// stream is up.
const (
	dialPreamble = 16 + 16 + 2
	dialOpened   = 1
)

var errDialFailed = errors.New("the fast relay reached the target neither by a fast path nor over the mesh")

// Dial gives a program of this node a connection to target, one of the fast
// ports, carried the way capture carries one: up a path of its channel, or
// over the mesh while none takes it. Capture needs nftables on Linux; this
// needs nothing. The program's end is a loopback connection whose other end
// goes through entryTCP, with this node's mesh address as its source; it is
// returned once the stream is up, ctx bounding the wait.
func (s *Service) Dial(ctx context.Context, target netip.AddrPort) (*net.TCPConn, error) {
	ln, key, err := s.dialListener()
	if err != nil {
		return nil, err
	}
	var d net.Dialer
	c, err := d.DialContext(ctx, "tcp", ln.Addr().String())
	if err != nil {
		return nil, err
	}
	tc := c.(*net.TCPConn)
	cancelDone := make(chan struct{})
	stopCancel := context.AfterFunc(ctx, func() { tc.Close(); close(cancelDone) })
	defer func() {
		if stopCancel != nil && !stopCancel() {
			<-cancelDone
		}
	}()
	_ = tc.SetNoDelay(true)
	pre := make([]byte, 0, dialPreamble)
	a := target.Addr().As16()
	pre = append(append(append(pre, key[:]...), a[:]...), byte(target.Port()>>8), byte(target.Port()))
	if _, err := tc.Write(pre); err != nil {
		tc.Close()
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		return nil, err
	}
	if dl, ok := ctx.Deadline(); ok {
		_ = tc.SetReadDeadline(dl)
	}
	var b [1]byte
	if _, err := io.ReadFull(tc, b[:]); err != nil || b[0] != dialOpened {
		tc.Close()
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		var ne net.Error
		if errors.As(err, &ne) && ne.Timeout() {
			return nil, err
		}
		return nil, errDialFailed
	}
	if !stopCancel() {
		<-cancelDone
		return nil, ctx.Err()
	}
	stopCancel = nil
	if err := ctx.Err(); err != nil {
		tc.Close()
		return nil, err
	}
	_ = tc.SetReadDeadline(time.Time{})
	return tc, nil
}

// PathRTT is the round trip of the path a new connection to target takes
// now, the first of its channel that answers; false while none does (the
// connections go over the mesh) or target is no fast port.
func (s *Service) PathRTT(target netip.AddrPort) (time.Duration, bool) {
	st, chans := s.state.Load(), s.live.Load()
	if st == nil || chans == nil {
		return 0, false
	}
	t, ok := st.target(target.Addr().Unmap(), target.Port(), protoTCP)
	if !ok {
		return 0, false
	}
	return chans.rtt(t.Node)
}

// dialListener is the loopback listener Dial connects to, opened the first
// time and kept until Close.
func (s *Service) dialListener() (net.Listener, [16]byte, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return nil, s.dialKey, net.ErrClosed
	}
	if s.dialLn != nil {
		return s.dialLn, s.dialKey, nil
	}
	if _, err := rand.Read(s.dialKey[:]); err != nil {
		return nil, s.dialKey, err
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return nil, s.dialKey, err
	}
	s.dialLn = ln
	go s.acceptDials(ln, s.dialKey)
	return ln, s.dialKey, nil
}

func (s *Service) acceptDials(ln net.Listener, key [16]byte) {
	for {
		c, err := ln.Accept()
		if err != nil {
			if errors.Is(err, net.ErrClosed) {
				return
			}
			time.Sleep(50 * time.Millisecond)
			continue
		}
		go s.serveDial(c.(*net.TCPConn), key)
	}
}

func (s *Service) serveDial(c *net.TCPConn, key [16]byte) {
	defer c.Close()
	var pre [dialPreamble]byte
	_ = c.SetReadDeadline(time.Now().Add(localDialTimeout))
	if _, err := io.ReadFull(c, pre[:]); err != nil || subtle.ConstantTimeCompare(pre[:16], key[:]) != 1 {
		return
	}
	_ = c.SetReadDeadline(time.Time{})
	_ = c.SetNoDelay(true)
	target := netip.AddrPortFrom(netip.AddrFrom16([16]byte(pre[16:32])).Unmap(), binary.BigEndian.Uint16(pre[32:]))
	from, _ := netip.ParseAddrPort(c.RemoteAddr().String())
	s.entryTCP(c, from, false, target, func() error {
		_, err := c.Write([]byte{dialOpened})
		return err
	})
}
