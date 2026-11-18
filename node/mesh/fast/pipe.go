package fast

import (
	"context"
	"io"
	"net"
	"net/netip"
	"time"
)

const (
	// fixedRcvBuf bounds receive buffers on loopback and local-entry sockets
	// before SYN/SYN-ACK so kernel auto-tuning does not grow them to megabytes
	// per stream on small-memory hosts.
	fixedRcvBuf      = 256 << 10
	localDialTimeout = 5 * time.Second
)

// abortTCP forces an immediate TCP RST (SO_LINGER=0) and discards unsent
// buffered bytes so every hop along the path releases its TCP memory at once.
func abortTCP(c *net.TCPConn) {
	if c == nil {
		return
	}
	_ = c.SetLinger(0)
	_ = c.Close()
}

// relayTCP copies both directions (using splice(2) on Linux between bare
// *net.TCPConn sockets). Normal EOF half-closes the write side so remaining
// data drains; any read/write error aborts both sides with TCP RST.
func relayTCP(a, b *net.TCPConn, aheadToB, aheadToA []byte) (toB, toA int64, aborted bool) {
	if len(aheadToB) > 0 {
		n, err := b.Write(aheadToB)
		toB += int64(n)
		if err != nil {
			abortTCP(a)
			abortTCP(b)
			return toB, 0, true
		}
	}
	if len(aheadToA) > 0 {
		n, err := a.Write(aheadToA)
		toA += int64(n)
		if err != nil {
			abortTCP(a)
			abortTCP(b)
			return toB, toA, true
		}
	}
	type res struct {
		n   int64
		err error
	}
	ch := make(chan res, 1)
	go func() {
		n, err := io.Copy(b, a)
		if err != nil {
			abortTCP(b)
			abortTCP(a)
		} else {
			_ = b.CloseWrite()
		}
		ch <- res{n: n, err: err}
	}()
	n2, err2 := io.Copy(a, b)
	if err2 != nil {
		abortTCP(a)
		abortTCP(b)
	} else {
		_ = a.CloseWrite()
	}
	r1 := <-ch
	toB += r1.n
	toA += n2
	if r1.err != nil || err2 != nil {
		abortTCP(a)
		abortTCP(b)
		return toB, toA, true
	}
	return toB, toA, false
}

// dialTCP connects to ap past any capture of this node's own, locking
// SO_RCVBUF to rcvBuf (if set) before connect() sends the SYN.
func dialTCP(ctx context.Context, ap netip.AddrPort, rcvBuf int) (*net.TCPConn, error) {
	d := &net.Dialer{
		Timeout:   localDialTimeout,
		KeepAlive: 15 * time.Second,
		Control:   controlFixedRcvBuf(rcvBuf),
	}
	var c net.Conn
	err := withFastSocket(func() error {
		var e error
		c, e = d.DialContext(ctx, "tcp4", ap.String())
		return e
	})
	if err != nil {
		return nil, err
	}
	tc := c.(*net.TCPConn)
	_ = tc.SetNoDelay(true)
	return tc, nil
}

func listenFixedTCP(network, address string, rcvBuf int) (net.Listener, error) {
	var lc net.ListenConfig
	if rcvBuf > 0 {
		lc.Control = controlFixedRcvBuf(rcvBuf)
	}
	var ln net.Listener
	err := withFastSocket(func() error {
		var e error
		ln, e = lc.Listen(context.Background(), network, address)
		return e
	})
	return ln, err
}
