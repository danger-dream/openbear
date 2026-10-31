package node

import (
	"bufio"
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

const (
	opContinuation = 0x0
	opText         = 0x1
	opBinary       = 0x2
	opClose        = 0x8
	opPing         = 0x9
	opPong         = 0xA

	maxMessage = 16 << 20
	// maxUpgradeResponse bounds the answer to the upgrade request, which may
	// come from anything on the path.
	maxUpgradeResponse = 64 << 10
	wsGUID             = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
)

// CloseError carries the peer's close code. Close frames are plaintext, so a
// code alone never counts as a revocation (see notice).
type CloseError struct {
	Code   int
	Reason string
}

func (e *CloseError) Error() string { return fmt.Sprintf("websocket closed: %d %s", e.Code, e.Reason) }

// Conn is a minimal RFC 6455 connection. Client connections mask outgoing
// frames; server connections (used by tests) do not.
type Conn struct {
	raw    net.Conn
	r      *bufio.Reader
	client bool
	wmu    sync.Mutex
	closed bool
	idle   time.Duration
}

func Dial(ctx context.Context, target string, header http.Header, timeout time.Duration) (*Conn, error) {
	u, err := url.Parse(target)
	if err != nil || (u.Scheme != "ws" && u.Scheme != "wss") || u.Host == "" {
		return nil, errors.New("invalid websocket url")
	}
	host := u.Host
	if u.Port() == "" {
		if u.Scheme == "wss" {
			host = net.JoinHostPort(u.Hostname(), "443")
		} else {
			host = net.JoinHostPort(u.Hostname(), "80")
		}
	}
	dialer := &net.Dialer{Timeout: timeout, KeepAlive: 30 * time.Second}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	var raw net.Conn
	if u.Scheme == "wss" {
		raw, err = (&tls.Dialer{NetDialer: dialer, Config: &tls.Config{ServerName: u.Hostname(), MinVersion: tls.VersionTLS12}}).DialContext(ctx, "tcp", host)
	} else {
		raw, err = dialer.DialContext(ctx, "tcp", host)
	}
	if err != nil {
		return nil, err
	}
	deadline, _ := ctx.Deadline()
	raw.SetDeadline(deadline)
	nonce := make([]byte, 16)
	rand.Read(nonce)
	key := base64.StdEncoding.EncodeToString(nonce)
	path := u.RequestURI()
	req := &http.Request{Method: http.MethodGet, URL: &url.URL{Path: u.Path, RawQuery: u.RawQuery}, Host: u.Host, Header: http.Header{},
		Proto: "HTTP/1.1", ProtoMajor: 1, ProtoMinor: 1}
	for k, v := range header {
		req.Header[k] = v
	}
	req.Header.Set("Upgrade", "websocket")
	req.Header.Set("Connection", "Upgrade")
	req.Header.Set("Sec-WebSocket-Key", key)
	req.Header.Set("Sec-WebSocket-Version", "13")
	var b strings.Builder
	fmt.Fprintf(&b, "GET %s HTTP/1.1\r\nHost: %s\r\n", path, u.Host)
	req.Header.Write(&b)
	b.WriteString("\r\n")
	if _, err = io.WriteString(raw, b.String()); err != nil {
		raw.Close()
		return nil, err
	}
	limited := &io.LimitedReader{R: raw, N: maxUpgradeResponse}
	r := bufio.NewReaderSize(limited, 64<<10)
	resp, err := http.ReadResponse(r, req)
	if err != nil {
		raw.Close()
		return nil, err
	}
	if resp.StatusCode != http.StatusSwitchingProtocols {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4<<10))
		resp.Body.Close()
		raw.Close()
		return nil, &HandshakeError{Status: resp.StatusCode, Body: body}
	}
	resp.Body.Close()
	sum := sha1.Sum([]byte(key + wsGUID))
	if !strings.EqualFold(resp.Header.Get("Upgrade"), "websocket") || resp.Header.Get("Sec-WebSocket-Accept") != base64.StdEncoding.EncodeToString(sum[:]) {
		raw.Close()
		return nil, errors.New("invalid websocket handshake")
	}
	raw.SetDeadline(time.Time{})
	limited.N = math.MaxInt64
	return &Conn{raw: raw, r: r, client: true}, nil
}

// HandshakeError is a refused upgrade; Body is the start of the response body.
type HandshakeError struct {
	Status int
	Body   []byte
}

func (e *HandshakeError) Error() string {
	return fmt.Sprintf("websocket handshake rejected: HTTP %d", e.Status)
}

// SetIdleTimeout bounds the wait for any frame, including the server's pings.
func (c *Conn) SetIdleTimeout(d time.Duration) { c.idle = d }

func (c *Conn) writeFrame(op byte, payload []byte) error {
	c.wmu.Lock()
	defer c.wmu.Unlock()
	if c.closed {
		return net.ErrClosed
	}
	header := make([]byte, 0, 14)
	header = append(header, 0x80|op)
	mask := byte(0)
	if c.client {
		mask = 0x80
	}
	switch n := len(payload); {
	case n < 126:
		header = append(header, mask|byte(n))
	case n <= 0xFFFF:
		header = append(header, mask|126, byte(n>>8), byte(n))
	default:
		header = append(header, mask|127)
		header = binary.BigEndian.AppendUint64(header, uint64(n))
	}
	data := payload
	if c.client {
		key := make([]byte, 4)
		rand.Read(key)
		header = append(header, key...)
		data = make([]byte, len(payload))
		for i, v := range payload {
			data[i] = v ^ key[i&3]
		}
	}
	c.raw.SetWriteDeadline(time.Now().Add(30 * time.Second))
	if _, err := c.raw.Write(append(header, data...)); err != nil {
		return err
	}
	if op == opClose {
		c.closed = true
	}
	return nil
}

func (c *Conn) WriteText(p []byte) error   { return c.writeFrame(opText, p) }
func (c *Conn) WriteBinary(p []byte) error { return c.writeFrame(opBinary, p) }

// Read returns the next complete data message, answering pings on the way.
func (c *Conn) Read() (op byte, payload []byte, err error) {
	var message []byte
	var messageOp byte
	for {
		if c.idle > 0 {
			c.raw.SetReadDeadline(time.Now().Add(c.idle))
		}
		var h [2]byte
		if _, err = io.ReadFull(c.r, h[:]); err != nil {
			return 0, nil, err
		}
		fin, op := h[0]&0x80 != 0, h[0]&0x0F
		if h[0]&0x70 != 0 {
			return 0, nil, c.fail(1002, "reserved bits set")
		}
		masked := h[1]&0x80 != 0
		if masked == c.client {
			return 0, nil, c.fail(1002, "bad masking")
		}
		n := uint64(h[1] & 0x7F)
		switch n {
		case 126:
			var ext [2]byte
			if _, err = io.ReadFull(c.r, ext[:]); err != nil {
				return 0, nil, err
			}
			n = uint64(binary.BigEndian.Uint16(ext[:]))
		case 127:
			var ext [8]byte
			if _, err = io.ReadFull(c.r, ext[:]); err != nil {
				return 0, nil, err
			}
			n = binary.BigEndian.Uint64(ext[:])
		}
		control := op >= 0x8
		if control && (n > 125 || !fin) {
			return 0, nil, c.fail(1002, "bad control frame")
		}
		if n > maxMessage || uint64(len(message))+n > maxMessage {
			return 0, nil, c.fail(1009, "message too large")
		}
		var key [4]byte
		if masked {
			if _, err = io.ReadFull(c.r, key[:]); err != nil {
				return 0, nil, err
			}
		}
		data := make([]byte, n)
		if _, err = io.ReadFull(c.r, data); err != nil {
			return 0, nil, err
		}
		if masked {
			for i := range data {
				data[i] ^= key[i&3]
			}
		}
		switch op {
		case opPing:
			if err = c.writeFrame(opPong, data); err != nil {
				return 0, nil, err
			}
			continue
		case opPong:
			continue
		case opClose:
			code, reason := 1005, ""
			if len(data) >= 2 {
				code, reason = int(binary.BigEndian.Uint16(data)), string(data[2:])
			}
			c.writeFrame(opClose, data[:min(len(data), 2)])
			c.raw.Close()
			return 0, nil, &CloseError{Code: code, Reason: reason}
		case opText, opBinary:
			if message != nil {
				return 0, nil, c.fail(1002, "unexpected new message")
			}
			messageOp, message = op, data
		case opContinuation:
			if message == nil {
				return 0, nil, c.fail(1002, "unexpected continuation")
			}
			message = append(message, data...)
		default:
			return 0, nil, c.fail(1002, "unknown opcode")
		}
		if fin {
			return messageOp, message, nil
		}
	}
}

func (c *Conn) fail(code int, reason string) error {
	c.CloseWith(code, reason)
	return &CloseError{Code: code, Reason: reason}
}

func (c *Conn) CloseWith(code int, reason string) {
	payload := binary.BigEndian.AppendUint16(nil, uint16(code))
	c.writeFrame(opClose, append(payload, reason...))
	c.raw.Close()
}

func (c *Conn) Close() error {
	c.CloseWith(1000, "")
	return nil
}

// accept upgrades a server-side connection; only tests use it.
func accept(w http.ResponseWriter, r *http.Request) (*Conn, error) {
	key := r.Header.Get("Sec-WebSocket-Key")
	if key == "" || !strings.EqualFold(r.Header.Get("Upgrade"), "websocket") {
		http.Error(w, "upgrade required", http.StatusBadRequest)
		return nil, errors.New("not a websocket request")
	}
	raw, rw, err := http.NewResponseController(w).Hijack()
	if err != nil {
		return nil, err
	}
	sum := sha1.Sum([]byte(key + wsGUID))
	fmt.Fprintf(rw, "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n", base64.StdEncoding.EncodeToString(sum[:]))
	if err = rw.Flush(); err != nil {
		raw.Close()
		return nil, err
	}
	return &Conn{raw: raw, r: rw.Reader}, nil
}
