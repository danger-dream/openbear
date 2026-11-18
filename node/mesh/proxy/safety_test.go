package proxy

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/netip"
	"strings"
	"testing"
	"time"
)

func TestHTTPHeaderBudgetBeforeAuthentication(t *testing.T) {
	for name, input := range map[string]string{
		"request-line": "GET /" + strings.Repeat("x", maxHTTPHeaderLine) + " HTTP/1.1\r\n\r\n",
		"single-field": "GET http://example.test/ HTTP/1.1\r\nX: " + strings.Repeat("x", maxHTTPHeaderLine) + "\r\n\r\n",
		"cumulative":   "GET http://example.test/ HTTP/1.1\r\n" + strings.Repeat("X: "+strings.Repeat("x", 1000)+"\r\n", 40) + "\r\n",
		"field-count":  "GET http://example.test/ HTTP/1.1\r\n" + strings.Repeat("X: x\r\n", maxHTTPHeaderLines) + "\r\n",
	} {
		t.Run(name, func(t *testing.T) {
			for _, size := range []int{1, 127, 4096} {
				t.Run(fmt.Sprint(size), func(t *testing.T) {
					source := &headerChunkReader{r: strings.NewReader(input), size: size}
					br := newProxyReader(source)
					req, err := br.readRequest()
					if req != nil || !errors.Is(err, errHTTPHeadersLarge) {
						t.Fatalf("unbounded header: request=%v error=%v", req, err)
					}
					// Parser lookahead must never consume the failure and let a
					// subsequent read resume from after the rejected bytes.
					left := source.r.Len()
					for i := 0; i < 2; i++ {
						var p [16]byte
						if n, err := br.budget.Read(p[:]); n != 0 || !errors.Is(err, errHTTPHeadersLarge) || source.r.Len() != left {
							t.Fatalf("header budget resumed: n=%d error=%v", n, err)
						}
					}
				})
			}
		})
	}
}

type headerChunkReader struct {
	r    *strings.Reader
	size int
}

func (r *headerChunkReader) Read(p []byte) (int, error) {
	return r.r.Read(p[:min(len(p), r.size)])
}

func TestHTTPHeaderBudgetDoesNotLimitOrLosePayload(t *testing.T) {
	payload := strings.Repeat("payload\r\n", maxHTTPHeaderBytes)
	for _, connect := range []bool{false, true} {
		header := fmt.Sprintf("POST http://example.test/ HTTP/1.1\r\nHost: example.test\r\nContent-Length: %d\r\n\r\n", len(payload))
		if connect {
			header = "CONNECT example.test:443 HTTP/1.1\r\nHost: example.test:443\r\n\r\n"
		}
		br := newProxyReader(strings.NewReader(header + payload))
		req, err := br.readRequest()
		if err != nil {
			t.Fatal(err)
		}
		var source io.Reader = req.Body
		if connect {
			source = br
		}
		got, err := io.ReadAll(source)
		if err != nil || string(got) != payload {
			t.Fatalf("connect=%v payload truncated: %d %v", connect, len(got), err)
		}
	}
	// SOCKS is not an HTTP request, even if its binary payload has many newlines.
	input := "\x05" + payload
	got, err := io.ReadAll(newProxyReader(strings.NewReader(input)))
	if err != nil || string(got) != input {
		t.Fatal("SOCKS limited", err)
	}
}

func TestOldExitTunnelsUseCurrentPolicy(t *testing.T) {
	from := netip.MustParseAddr("100.96.0.2")
	lan, public := netip.MustParseAddr("192.168.10.9"), netip.MustParseAddr("203.0.113.9")
	local := &localAddrs{at: time.Now(), addrs: map[netip.Addr]bool{}}
	old := &policy{lan: true, self: local}
	open := &conns{}
	open.updateExit(old, []netip.Addr{from})
	s := &exitServer{policy: old, conns: open, stats: &exitStats{}}
	newConn := func() (net.Conn, net.Conn) {
		a, b := net.Pipe()
		t.Cleanup(func() { a.Close(); b.Close() })
		if !open.add(a, from) {
			t.Fatal("add")
		}
		return a, b
	}
	warm, _ := newConn() // no target header has been received yet
	udp, _ := newConn()  // UDP associations never have one fixed reached target
	allowed, peer := newConn()
	if !open.reached(allowed, public) {
		t.Fatal("public target refused")
	}
	disallowed, deniedPeer := newConn()
	open.reached(disallowed, lan)
	cached := map[string]netip.Addr{"nas.example.test": lan, "public.example.test": public}
	if _, ok := s.udpTarget(Addr{Host: "nas.example.test", Port: 445}, cached); !ok {
		t.Fatal("old LAN policy")
	}
	open.updateExit(&policy{self: local}, []netip.Addr{from})
	if _, code := s.resolve(context.Background(), Addr{IP: lan, Port: 445}); code != statusNotAllowed {
		t.Fatal("prewarmed tunnel retained old LAN policy")
	}
	for _, a := range []Addr{{IP: lan, Port: 445}, {Host: "nas.example.test", Port: 445}} {
		if _, ok := s.udpTarget(a, cached); ok {
			t.Fatal("UDP cached/literal targets use stale policy")
		}
	}
	if got, ok := s.udpTarget(Addr{Host: "public.example.test", Port: 443}, cached); !ok || got != public {
		t.Fatal("unrelated cached target lost")
	}
	if open.reached(warm, lan) {
		t.Fatal("dial begun before update escaped revocation")
	}
	if !open.reached(udp, public) {
		t.Fatal("unaffected tunnel was lost")
	}
	deniedPeer.SetReadDeadline(time.Now().Add(time.Second))
	if _, err := deniedPeer.Read(make([]byte, 1)); err != io.EOF {
		t.Fatal("established LAN connection not closed", err)
	}
	peer.SetReadDeadline(time.Now().Add(time.Millisecond))
	if _, err := peer.Read(make([]byte, 1)); err == io.EOF {
		t.Fatal("unrelated public connection was closed")
	}
	open.updateExit(nil, nil)
	if s.allowed(public) {
		t.Fatal("disabled exit permits new targets")
	}
}

func TestReplacingUDPHubEndsControlSession(t *testing.T) {
	control, client := net.Pipe()
	tunnel, exit := net.Pipe()
	defer client.Close()
	defer exit.Close()
	h := &udpHub{assocs: map[*udpAssoc]bool{}}
	if !h.add(&udpAssoc{control: control, tunnel: tunnel}) {
		t.Fatal("register")
	}
	s := &entryServer{hubs: map[netip.AddrPort]*udpHub{{}: h}, stop: make(chan struct{})}
	s.close() // the actual entry replacement closes hubs through this path
	for _, c := range []net.Conn{client, exit} {
		c.SetReadDeadline(time.Now().Add(time.Second))
		if _, err := c.Read(make([]byte, 1)); err != io.EOF {
			t.Fatal("association left silently alive", err)
		}
	}
	if h.add(&udpAssoc{}) {
		t.Fatal("in-flight association joined a dead hub")
	}
}
