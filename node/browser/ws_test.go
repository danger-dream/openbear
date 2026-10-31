package browser

import (
	"bufio"
	"context"
	"crypto/sha1"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

// serverWS is a minimal RFC 6455 server side for tests.
type serverWS struct {
	c     net.Conn
	r     *bufio.Reader
	wmu   sync.Mutex
	pongs chan []byte
}

func (s *serverWS) read() ([]byte, byte, error) {
	var h [2]byte
	if _, err := io.ReadFull(s.r, h[:]); err != nil {
		return nil, 0, err
	}
	op := h[0] & 0x0F
	n := uint64(h[1] & 0x7F)
	switch n {
	case 126:
		var ext [2]byte
		io.ReadFull(s.r, ext[:])
		n = uint64(binary.BigEndian.Uint16(ext[:]))
	case 127:
		var ext [8]byte
		io.ReadFull(s.r, ext[:])
		n = binary.BigEndian.Uint64(ext[:])
	}
	var key [4]byte
	if h[1]&0x80 != 0 {
		io.ReadFull(s.r, key[:])
	}
	payload := make([]byte, n)
	if _, err := io.ReadFull(s.r, payload); err != nil {
		return nil, 0, err
	}
	for i := range payload {
		payload[i] ^= key[i&3]
	}
	return payload, op, nil
}

func (s *serverWS) write(op byte, payload []byte, fin bool) error {
	s.wmu.Lock()
	defer s.wmu.Unlock()
	b := byte(op)
	if fin {
		b |= 0x80
	}
	header := []byte{b}
	switch n := len(payload); {
	case n < 126:
		header = append(header, byte(n))
	case n <= 0xFFFF:
		header = append(header, 126, byte(n>>8), byte(n))
	default:
		header = append(header, 127)
		header = binary.BigEndian.AppendUint64(header, uint64(n))
	}
	_, err := s.c.Write(append(header, payload...))
	return err
}

func (s *serverWS) json(v any) error {
	body, _ := json.Marshal(v)
	return s.write(0x1, body, true)
}

// cdpServer serves a fake CDP websocket; handle gets every decoded command.
func cdpServer(t *testing.T, handle func(s *serverWS, msg map[string]any)) string {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Upgrade") != "websocket" {
			http.NotFound(w, r)
			return
		}
		sum := sha1.Sum([]byte(r.Header.Get("Sec-WebSocket-Key") + wsGUID))
		c, rw, err := w.(http.Hijacker).Hijack()
		if err != nil {
			return
		}
		defer c.Close()
		rw.WriteString("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " +
			base64.StdEncoding.EncodeToString(sum[:]) + "\r\n\r\n")
		rw.Flush()
		s := &serverWS{c: c, r: rw.Reader, pongs: make(chan []byte, 4)}
		for {
			payload, op, err := s.read()
			if err != nil || op == 0x8 {
				return
			}
			if op == 0xA {
				s.pongs <- payload
			}
			if op != 0x1 {
				continue
			}
			var msg map[string]any
			json.Unmarshal(payload, &msg)
			handle(s, msg)
		}
	}))
	t.Cleanup(srv.Close)
	return "ws://" + srv.Listener.Addr().String() + "/devtools/browser/test"
}

func TestWebSocketRoundTripFragmentsPingAndLargeMessages(t *testing.T) {
	big := strings.Repeat("x", 5<<20)
	server := make(chan *serverWS, 1)
	url := cdpServer(t, func(s *serverWS, msg map[string]any) {
		switch msg["method"] {
		case "big":
			body, _ := json.Marshal(map[string]any{"id": msg["id"], "result": map[string]any{"data": big}})
			third := len(body) / 3
			s.write(0x2, []byte("binary-ignored"), true)
			s.write(0x1, body[:third], false)
			s.write(0x9, []byte("hb"), true) // control frame between fragments
			s.write(0x0, body[third:2*third], false)
			s.write(0x0, body[2*third:], true)
			server <- s
		default:
			s.json(map[string]any{"id": msg["id"], "result": map[string]any{"method": msg["method"]}})
		}
	})
	tr, err := DialWebSocket(context.Background(), url)
	if err != nil {
		t.Fatal(err)
	}
	c := newConn(tr, 5*time.Second, func(string, *Obj, string) {})
	defer c.close()
	r, err := c.call(context.Background(), "small", nil, "", time.Second)
	if err != nil || pyStr(r.Must("method")) != "small" {
		t.Fatalf("%v %v", r, err)
	}
	r, err = c.call(context.Background(), "big", nil, "", 5*time.Second)
	if err != nil || asStr(r.Must("data")) != big {
		t.Fatalf("big: %v", err)
	}
	s := <-server
	select {
	case p := <-s.pongs:
		if string(p) != "hb" {
			t.Fatalf("pong %q", p)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("ping not answered")
	}
}

func TestWebSocketMessageLimitAndHandshakeFailure(t *testing.T) {
	url := cdpServer(t, func(s *serverWS, msg map[string]any) {
		size := 4096
		if msg["method"] == "small" {
			size = 10
		}
		s.json(map[string]any{"id": msg["id"], "result": map[string]any{"data": strings.Repeat("y", size)}})
	})
	ws, err := dialWebSocket(context.Background(), url, 1024)
	if err != nil {
		t.Fatal(err)
	}
	c := newConn(ws, time.Second, func(string, *Obj, string) {})
	if _, err := c.call(context.Background(), "x", nil, "", time.Second); err == nil || err.Error() != "cdp_message_too_large" {
		t.Fatalf("oversized message: %v", err)
	}
	// Only that message was dropped; the connection carries on.
	if r, err := c.call(context.Background(), "small", nil, "", time.Second); err != nil || len(asStr(r.Must("data"))) != 10 {
		t.Fatalf("after oversized message: %v", err)
	}
	c.close()
	if _, err := DialWebSocket(context.Background(), strings.Replace(url, "ws://", "http://", 1)); err == nil {
		t.Fatal("non-websocket scheme accepted")
	}
	srv := httptest.NewServer(http.NotFoundHandler())
	defer srv.Close()
	if _, err := DialWebSocket(context.Background(), "ws://"+srv.Listener.Addr().String()+"/x"); err == nil {
		t.Fatal("rejected handshake accepted")
	}
	if maxMessage != 512*1024*1024*4/3+2*1024*1024 {
		t.Fatal("message ceiling changed")
	}
}

func TestInitializeResolvesHTTPEndpointThroughJSONVersion(t *testing.T) {
	ws := cdpServer(t, func(s *serverWS, msg map[string]any) {
		s.json(map[string]any{"id": msg["id"], "result": map[string]any{}})
	})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/json/version" {
			http.NotFound(w, r)
			return
		}
		json.NewEncoder(w).Encode(map[string]any{"webSocketDebuggerUrl": ws})
	}))
	defer srv.Close()
	e := New()
	defer e.Close()
	result, err := e.Execute(context.Background(), "initialize", raw(map[string]any{
		"endpoint": srv.URL, "connectTimeoutS": 2, "downloadHostPath": t.TempDir(),
	}))
	if err != nil || dumpsASCII(result) != `{"connected": true, "engine": "go-cdp"}` {
		t.Fatalf("%v %v", result, err)
	}
}
