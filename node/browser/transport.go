package browser

import (
	"bufio"
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha1"
	"crypto/tls"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"
)

// maxMessage is the hard CDP message ceiling: the public maximum artifact
// size after base64 plus protocol overhead (512MB*4/3 + 2MB). Only replies
// to calls that fetch an artifact may reach it.
const maxMessage = 512*1024*1024*4/3 + 2*1024*1024

const (
	// maxEvent bounds what a page can push unasked (console arguments, binding
	// payloads). It still carries Chrome's longest URL (2MB) in navigation and
	// request events.
	maxEvent = 4 << 20
	// maxReply bounds the reply to any other call.
	maxReply = 64 << 20
	// messageHead is how much of a message decides its ceiling.
	messageHead = 64
	// maxHandshake bounds the websocket upgrade response.
	maxHandshake = 64 << 10
)

// Transport carries complete CDP JSON messages. Receive reports a message
// over its ceiling as an error wrapping errMessageTooLarge (a
// *droppedMessage); the transport stays usable after it.
type Transport interface {
	Send(ctx context.Context, msg []byte) error
	Receive() ([]byte, error)
	Close() error
}

var errMessageTooLarge = errors.New("cdp message too large")

// droppedMessage is a message discarded while it was read because it
// outgrew its ceiling; head is its first bytes.
type droppedMessage struct{ head []byte }

func (d *droppedMessage) Error() string { return errMessageTooLarge.Error() }
func (d *droppedMessage) Unwrap() error { return errMessageTooLarge }

// sizer gives the ceiling of a message from its first bytes. The
// transports here consult it only for a message larger than maxEvent.
type sizer func(head []byte) int

// sized is implemented by this package's transports.
type sized interface{ setSizer(sizer) }

// ceilings returns the size up to which a message needs no decision and the
// ceiling of one starting with head; hard bounds both.
func ceilings(size sizer, hard int) (int, func(head []byte) int) {
	if size == nil {
		return hard, func([]byte) int { return hard }
	}
	return min(maxEvent, hard), func(head []byte) int { return min(size(head), hard) }
}

// headOf is the first messageHead bytes of message followed by chunk.
func headOf(message, chunk []byte) []byte {
	head := append([]byte(nil), message[:min(len(message), messageHead)]...)
	return append(head, chunk[:min(len(chunk), messageHead-len(head))]...)
}

// ------------------------------------------------------------- websocket

const wsGUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

type wsConn struct {
	raw       net.Conn
	r         *bufio.Reader
	wmu       sync.Mutex
	closed    chan struct{}
	closeOnce sync.Once
	limit     int
	size      sizer
	activity  chan struct{}
}

func (c *wsConn) setSizer(s sizer) { c.size = s }

// DialWebSocket opens a minimal RFC 6455 client connection to a ws:// or
// wss:// CDP endpoint. No proxy settings are consulted. A 20s heartbeat
// closes a peer that stops answering pings.
func DialWebSocket(ctx context.Context, target string) (Transport, error) {
	return dialWebSocket(ctx, target, maxMessage)
}

func dialWebSocket(ctx context.Context, target string, limit int) (*wsConn, error) {
	u, err := url.Parse(target)
	if err != nil || (u.Scheme != "ws" && u.Scheme != "wss") || u.Host == "" {
		return nil, errors.New("invalid websocket url")
	}
	host := u.Host
	if u.Port() == "" {
		port := "80"
		if u.Scheme == "wss" {
			port = "443"
		}
		host = net.JoinHostPort(u.Hostname(), port)
	}
	dialer := &net.Dialer{KeepAlive: 30 * time.Second}
	var raw net.Conn
	if u.Scheme == "wss" {
		raw, err = (&tls.Dialer{NetDialer: dialer, Config: &tls.Config{ServerName: u.Hostname(), MinVersion: tls.VersionTLS12}}).DialContext(ctx, "tcp", host)
	} else {
		raw, err = dialer.DialContext(ctx, "tcp", host)
	}
	if err != nil {
		return nil, err
	}
	stop := context.AfterFunc(ctx, func() { raw.SetDeadline(time.Unix(1, 0)) })
	conn, err := handshake(raw, u)
	if !stop() || err != nil {
		raw.Close()
		if err == nil {
			err = ctx.Err()
		}
		return nil, err
	}
	raw.SetDeadline(time.Time{})
	c := &wsConn{raw: raw, r: conn, closed: make(chan struct{}), limit: limit, activity: make(chan struct{}, 1)}
	go c.heartbeat()
	return c, nil
}

func handshake(raw net.Conn, u *url.URL) (*bufio.Reader, error) {
	nonce := make([]byte, 16)
	rand.Read(nonce)
	key := base64.StdEncoding.EncodeToString(nonce)
	path := u.RequestURI()
	var b strings.Builder
	fmt.Fprintf(&b, "GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n", path, u.Host, key)
	if u.User != nil {
		pass, _ := u.User.Password()
		fmt.Fprintf(&b, "Authorization: Basic %s\r\n", base64.StdEncoding.EncodeToString([]byte(u.User.Username()+":"+pass)))
	}
	b.WriteString("\r\n")
	if _, err := io.WriteString(raw, b.String()); err != nil {
		return nil, err
	}
	// The bound ends with the handshake; frames are bounded by Receive.
	limited := &io.LimitedReader{R: raw, N: maxHandshake}
	r := bufio.NewReaderSize(limited, 64<<10)
	resp, err := http.ReadResponse(r, &http.Request{Method: http.MethodGet})
	if err != nil {
		if limited.N <= 0 {
			return nil, errors.New("websocket handshake response too large")
		}
		return nil, err
	}
	limited.N = math.MaxInt64
	resp.Body.Close()
	if resp.StatusCode != http.StatusSwitchingProtocols {
		return nil, fmt.Errorf("websocket handshake rejected: HTTP %d", resp.StatusCode)
	}
	sum := sha1.Sum([]byte(key + wsGUID))
	if !strings.EqualFold(resp.Header.Get("Upgrade"), "websocket") || resp.Header.Get("Sec-WebSocket-Accept") != base64.StdEncoding.EncodeToString(sum[:]) {
		return nil, errors.New("invalid websocket handshake")
	}
	return r, nil
}

func (c *wsConn) writeFrame(op byte, payload []byte, deadline time.Time) error {
	c.wmu.Lock()
	defer c.wmu.Unlock()
	select {
	case <-c.closed:
		return net.ErrClosed
	default:
	}
	header := make([]byte, 0, 14)
	header = append(header, 0x80|op)
	switch n := len(payload); {
	case n < 126:
		header = append(header, 0x80|byte(n))
	case n <= 0xFFFF:
		header = append(header, 0x80|126, byte(n>>8), byte(n))
	default:
		header = append(header, 0x80|127)
		header = binary.BigEndian.AppendUint64(header, uint64(n))
	}
	var key [4]byte
	rand.Read(key[:])
	header = append(header, key[:]...)
	frame := make([]byte, len(header)+len(payload))
	copy(frame, header)
	data := frame[len(header):]
	for i, v := range payload {
		data[i] = v ^ key[i&3]
	}
	if deadline.IsZero() {
		deadline = time.Now().Add(30 * time.Second)
	}
	c.raw.SetWriteDeadline(deadline)
	if _, err := c.raw.Write(frame); err != nil {
		// A partially written frame corrupts the stream.
		c.shutdown()
		return err
	}
	return nil
}

// Send writes one text message. A started frame is always completed (or the
// connection dropped): an interrupted frame would corrupt the stream.
func (c *wsConn) Send(ctx context.Context, msg []byte) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	return c.writeFrame(0x1, msg, time.Time{})
}

func (c *wsConn) touch() {
	select {
	case c.activity <- struct{}{}:
	default:
	}
}

// heartbeat follows aiohttp's heartbeat=20: ping after 20s without traffic,
// close if nothing arrives within the following 10s.
func (c *wsConn) heartbeat() {
	idle := time.NewTimer(20 * time.Second)
	defer idle.Stop()
	for {
		select {
		case <-c.closed:
			return
		case <-c.activity:
			if !idle.Stop() {
				select {
				case <-idle.C:
				default:
				}
			}
			idle.Reset(20 * time.Second)
		case <-idle.C:
			if c.writeFrame(0x9, nil, time.Now().Add(10*time.Second)) != nil {
				return
			}
			select {
			case <-c.closed:
				return
			case <-c.activity:
				idle.Reset(20 * time.Second)
			case <-time.After(10 * time.Second):
				c.shutdown()
				return
			}
		}
	}
}

// Receive returns the next complete text message; pings are answered and
// binary messages skipped. A message over its ceiling is discarded frame by
// frame as it arrives, never buffered whole.
func (c *wsConn) Receive() ([]byte, error) {
	var message, head []byte
	var messageOp byte
	small, ceiling := ceilings(c.size, c.limit)
	limit, dropping := 0, false
	for {
		var h [2]byte
		if _, err := io.ReadFull(c.r, h[:]); err != nil {
			c.shutdown()
			return nil, err
		}
		c.touch()
		fin, op := h[0]&0x80 != 0, h[0]&0x0F
		if h[0]&0x70 != 0 || h[1]&0x80 != 0 {
			c.shutdown()
			return nil, errors.New("websocket protocol error")
		}
		n := uint64(h[1] & 0x7F)
		switch n {
		case 126:
			var ext [2]byte
			if _, err := io.ReadFull(c.r, ext[:]); err != nil {
				c.shutdown()
				return nil, err
			}
			n = uint64(binary.BigEndian.Uint16(ext[:]))
		case 127:
			var ext [8]byte
			if _, err := io.ReadFull(c.r, ext[:]); err != nil {
				c.shutdown()
				return nil, err
			}
			n = binary.BigEndian.Uint64(ext[:])
			if n>>63 != 0 {
				c.shutdown()
				return nil, errors.New("websocket protocol error")
			}
		}
		if op >= 0x8 {
			if n > 125 || !fin {
				c.shutdown()
				return nil, errors.New("websocket protocol error")
			}
			payload := make([]byte, n)
			if _, err := io.ReadFull(c.r, payload); err != nil {
				c.shutdown()
				return nil, err
			}
			switch op {
			case 0x8:
				c.writeFrame(0x8, payload[:min(len(payload), 2)], time.Now().Add(time.Second))
				c.shutdown()
				return nil, io.EOF
			case 0x9:
				c.writeFrame(0xA, payload, time.Time{})
			}
			continue
		}
		if op != 0x0 {
			messageOp = op
		}
		if !dropping && limit == 0 && uint64(len(message))+n > uint64(small) {
			// The ceiling depends on the first bytes; read them before the rest.
			if take := min(n, uint64(max(messageHead-len(message), 0))); take > 0 {
				start := len(message)
				message = append(message, make([]byte, take)...)
				if _, err := io.ReadFull(c.r, message[start:]); err != nil {
					c.shutdown()
					return nil, err
				}
				n -= take
			}
			head = headOf(message, nil)
			limit = ceiling(head)
		}
		if !dropping && limit > 0 && uint64(len(message))+n > uint64(limit) {
			dropping, message = true, nil
		}
		if dropping {
			if _, err := io.CopyN(io.Discard, c.r, int64(n)); err != nil {
				c.shutdown()
				return nil, err
			}
		} else {
			start := len(message)
			message = append(message, make([]byte, n)...)
			if _, err := io.ReadFull(c.r, message[start:]); err != nil {
				c.shutdown()
				return nil, err
			}
		}
		if fin {
			switch {
			case messageOp == 0x1 && dropping:
				return nil, &droppedMessage{head}
			case messageOp == 0x1:
				return message, nil
			}
			message, head, messageOp, limit, dropping = nil, nil, 0, 0, false
		}
	}
}

func (c *wsConn) shutdown() {
	c.closeOnce.Do(func() {
		close(c.closed)
		c.raw.Close()
	})
}

func (c *wsConn) Close() error {
	select {
	case <-c.closed:
		return nil
	default:
	}
	c.writeFrame(0x8, []byte{0x03, 0xE8}, time.Now().Add(time.Second))
	c.shutdown()
	return nil
}

// ------------------------------------------------------------------ pipe

type pipeTransport struct {
	r      *bufio.Reader
	w      io.Writer
	closer io.Closer
	wmu    sync.Mutex
	once   sync.Once
	closed chan struct{}
	limit  int
	size   sizer
}

func (p *pipeTransport) setSizer(s sizer) { p.size = s }

// NewPipe wraps Chrome's --remote-debugging-pipe streams: every message is
// a JSON text terminated by a NUL byte.
func NewPipe(r io.Reader, w io.Writer, closer io.Closer) Transport {
	return &pipeTransport{r: bufio.NewReaderSize(r, 64<<10), w: w, closer: closer, closed: make(chan struct{}), limit: maxMessage}
}

func (p *pipeTransport) Send(ctx context.Context, msg []byte) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	p.wmu.Lock()
	defer p.wmu.Unlock()
	select {
	case <-p.closed:
		return net.ErrClosed
	default:
	}
	buf := make([]byte, len(msg)+1)
	copy(buf, msg)
	_, err := p.w.Write(buf)
	return err
}

// Receive returns the next message; one over its ceiling is discarded in
// buffer-sized pieces up to its terminator, never held whole.
func (p *pipeTransport) Receive() ([]byte, error) {
	var message, head []byte
	small, ceiling := ceilings(p.size, p.limit)
	limit, dropping := 0, false
	for {
		chunk, err := p.r.ReadSlice(0)
		end := err == nil
		if !end && !errors.Is(err, bufio.ErrBufferFull) {
			return nil, err
		}
		if end {
			chunk = chunk[:len(chunk)-1]
		}
		if !dropping && limit == 0 && len(message)+len(chunk) > small {
			head = headOf(message, chunk)
			limit = ceiling(head)
		}
		if !dropping && limit > 0 && len(message)+len(chunk) > limit {
			dropping, message = true, nil
		}
		if !dropping {
			message = append(message, chunk...)
		}
		if !end {
			continue
		}
		if dropping {
			return nil, &droppedMessage{head}
		}
		if len(bytes.TrimSpace(message)) == 0 {
			message, limit = message[:0], 0
			continue
		}
		return message, nil
	}
}

func (p *pipeTransport) Close() error {
	var err error
	p.once.Do(func() {
		close(p.closed)
		if p.closer != nil {
			err = p.closer.Close()
		}
	})
	return err
}
