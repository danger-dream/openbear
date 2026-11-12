package node

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"net/url"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestLatencyWatchParams(t *testing.T) {
	good := `{"targets": [{"id": "yn-ct", "host": "yn-ct-v4.ip.zstaticcdn.com", "port": 80, "mode": "tcp"},
		{"id": "ali-dns", "host": "223.5.5.5", "mode": "icmp"}, {"id": "v6", "host": "2001:db8::1", "port": 443, "mode": "tcp"},
		{"id": "web", "host": "Example.com", "mode": "http", "url": "https://example.com:8443/health"}], "intervalMs": 10000}`
	w, err := parseLatencyWatch(json.RawMessage(good), nil)
	if err != nil || len(w.Targets) != 4 || w.Interval != 10*time.Second {
		t.Fatalf("good: %+v %v", w, err)
	}
	for name, raw := range map[string]string{
		"interval too short": `{"targets": [], "intervalMs": 2999}`,
		"interval too long":  `{"targets": [], "intervalMs": 600001}`,
		"tcp without port":   `{"targets": [{"id": "a", "host": "h.example", "mode": "tcp"}], "intervalMs": 3000}`,
		"unknown mode":       `{"targets": [{"id": "a", "host": "h.example", "port": 1, "mode": "udp"}], "intervalMs": 3000}`,
		"same id twice":      `{"targets": [{"id": "a", "host": "h", "mode": "icmp"}, {"id": "a", "host": "g", "mode": "icmp"}], "intervalMs": 3000}`,
		"host with a path":   `{"targets": [{"id": "a", "host": "h/x", "mode": "icmp"}], "intervalMs": 3000}`,
		"no id":              `{"targets": [{"id": "", "host": "h", "mode": "icmp"}], "intervalMs": 3000}`,
		"http without url":   `{"targets": [{"id": "a", "host": "h.example", "mode": "http"}], "intervalMs": 3000}`,
		"http not http":      `{"targets": [{"id": "a", "host": "h.example", "mode": "http", "url": "ftp://h.example/"}], "intervalMs": 3000}`,
		"http elsewhere":     `{"targets": [{"id": "a", "host": "h.example", "mode": "http", "url": "https://g.example/"}], "intervalMs": 3000}`,
		"not json":           `[`,
	} {
		if _, err := parseLatencyWatch(json.RawMessage(raw), nil); !errors.Is(err, errInvalidLatency) {
			t.Errorf("%s: %v", name, err)
		}
	}
	// Nothing to measure is fine: it stops the watch.
	if w, err := parseLatencyWatch(json.RawMessage(`{"targets": [], "intervalMs": 3000}`), nil); err != nil || len(w.Targets) != 0 {
		t.Fatalf("empty: %v", err)
	}
}

func TestLatencyFrontMeasuresItsExitsOnly(t *testing.T) {
	allowed := frontHosts(Config{Front: true, URL: "https://10.0.10.246:52002", Backup: "https://[2001:db8::9]:52002"})
	ok := `{"targets": [{"id": "exit-1", "host": "10.0.10.246", "port": 52002, "mode": "tcp"}, {"id": "exit-2", "host": "2001:db8::9", "port": 52002, "mode": "tcp"}], "intervalMs": 10000}`
	if _, err := parseLatencyWatch(json.RawMessage(ok), allowed); err != nil {
		t.Fatal(err)
	}
	other := `{"targets": [{"id": "yn-ct", "host": "yn-ct-v4.ip.zstaticcdn.com", "port": 80, "mode": "tcp"}], "intervalMs": 10000}`
	if _, err := parseLatencyWatch(json.RawMessage(other), allowed); err == nil || err.Error() != "front_target_refused" {
		t.Fatalf("a front measured elsewhere: %v", err)
	}
	if frontHosts(Config{URL: "https://example.com"}) != nil {
		t.Fatal("not a front: no restriction")
	}
}

// HTTP: the time from the request written to the answer's first byte, over a connection to the
// address resolved; any answer counts, a redirect is not followed, a certificate is not checked.
func TestLatencyHTTPProbe(t *testing.T) {
	var followed atomic.Int32
	mux := http.NewServeMux()
	mux.HandleFunc("/slow", func(w http.ResponseWriter, _ *http.Request) {
		time.Sleep(40 * time.Millisecond)
		w.WriteHeader(http.StatusNotFound)
	})
	mux.HandleFunc("/moved", func(w http.ResponseWriter, r *http.Request) { http.Redirect(w, r, "/elsewhere", http.StatusFound) })
	mux.HandleFunc("/elsewhere", func(http.ResponseWriter, *http.Request) { followed.Add(1) })
	plain, secure := httptest.NewServer(mux), httptest.NewTLSServer(mux)
	defer plain.Close()
	defer secure.Close()
	w := newLatencyWatcher()
	var dialled []string
	var mu sync.Mutex
	dial := w.dial
	w.dial = func(ctx context.Context, network, address string) (net.Conn, error) {
		mu.Lock()
		dialled = append(dialled, network+" "+address)
		mu.Unlock()
		return dial(ctx, network, address)
	}
	probe := func(raw string) (time.Duration, bool, *latencyProbe) {
		u, _ := url.Parse(raw)
		p := &latencyProbe{w: w, t: latencyTarget{ID: "web", Host: u.Hostname(), Mode: "http", URL: raw}}
		rtt, ok := p.probe(context.Background(), latencyHTTPTimeout)
		return rtt, ok, p
	}
	rtt, ok, p := probe(plain.URL + "/slow")
	if !ok || rtt < 40*time.Millisecond || rtt > time.Second {
		t.Fatalf("a 404 after 40 ms: %v %v", rtt, ok)
	}
	// The next probe uses the connection kept, with no new dial.
	if rtt, ok := p.probe(context.Background(), latencyHTTPTimeout); !ok || rtt < 40*time.Millisecond || len(dialled) != 1 {
		t.Fatalf("again: %v %v %v", rtt, ok, dialled)
	}
	p.close()
	if _, ok, _ := probe(plain.URL + "/moved"); !ok || followed.Load() != 0 {
		t.Fatalf("redirect: %v, followed %d", ok, followed.Load())
	}
	if _, ok, _ := probe(secure.URL + "/slow"); !ok {
		t.Fatal("a self-signed certificate is not checked")
	}
	down := plain.URL
	plain.Close()
	if _, ok, _ := probe(down + "/slow"); ok {
		t.Fatal("nothing listening: lost")
	}
	// Too slow for the timeout: lost.
	u, _ := url.Parse(secure.URL + "/slow")
	q := &latencyProbe{w: w, t: latencyTarget{ID: "web", Host: u.Hostname(), Mode: "http", URL: u.String()}}
	if _, ok := q.probe(context.Background(), 20*time.Millisecond); ok {
		t.Fatal("slower than the timeout: lost")
	}
}

type fakeConn struct{ net.Conn }

func (fakeConn) Close() error { return nil }

// A fake clock and dial: each probe moves the clock 20 s on, so three probes make a minute; every
// fourth dial fails.
func TestLatencyMinutesSummed(t *testing.T) {
	var mu sync.Mutex
	clock := time.Unix(1_800_000_000-1_800_000_000%60, 0)
	dials := 0
	w := &latencyWatcher{
		now: func() time.Time { mu.Lock(); defer mu.Unlock(); return clock },
		dial: func(ctx context.Context, network, address string) (net.Conn, error) {
			mu.Lock()
			defer mu.Unlock()
			dials++
			clock = clock.Add(20 * time.Second)
			if address != "192.0.2.7:443" || network != "tcp4" {
				t.Errorf("dialled %s %s", network, address)
			}
			if dials%4 == 0 {
				return nil, errors.New("refused")
			}
			return fakeConn{}, nil
		},
		lookupIP: func(ctx context.Context, network, host string) ([]netip.Addr, error) {
			if network != "ip4" || host != "lat.example" {
				return nil, errors.New("no such host")
			}
			return []netip.Addr{netip.MustParseAddr("192.0.2.7")}, nil
		},
	}
	out := make(chan latencyMinute, 8)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go w.run(ctx, latencyTarget{ID: "x", Host: "lat.example", Port: 443, Mode: "tcp"}, 5*time.Millisecond, func(m latencyMinute) {
		select {
		case out <- m:
		default:
		}
	})
	got := []latencyMinute{<-out, <-out}
	cancel()
	for i, m := range got {
		if m.Target != "x" || m.TS%60 != 0 || m.Sent == 0 || m.Avg == nil || *m.Min > *m.Avg || *m.Avg > *m.Max {
			t.Fatalf("minute %d: %+v", i, m)
		}
	}
	if got[1].TS != got[0].TS+60 || got[0].Sent+got[1].Sent < 5 || got[0].Lost+got[1].Lost < 1 {
		t.Fatalf("minutes: %+v %+v", got[0], got[1])
	}
	// A minute with nothing answered has no latency.
	all := (&latencyAcc{ts: 60}).minute("y")
	acc := latencyAcc{ts: 60}
	acc.add(0, false)
	if m := acc.minute("y"); m.Sent != 1 || m.Lost != 1 || m.Avg != nil || all.Avg != nil {
		t.Fatalf("all lost: %+v", m)
	}
}

func TestLatencyLostWhenTheNameDoesNotResolve(t *testing.T) {
	w := &latencyWatcher{now: time.Now,
		lookupIP: func(context.Context, string, string) ([]netip.Addr, error) { return nil, errors.New("nxdomain") },
		dial: func(context.Context, string, string) (net.Conn, error) {
			t.Error("dialled without an address")
			return nil, errors.New("no")
		}}
	p := &latencyProbe{w: w, t: latencyTarget{ID: "x", Host: "gone.example", Port: 80, Mode: "tcp"}}
	if _, ok := p.probe(context.Background(), time.Second); ok {
		t.Fatal("a name that does not resolve is a lost probe")
	}
}

// A TCPing waits for its one SYN only: a connection made after the kernel sent it again is a lost probe.
func TestLatencyTCPingWaitsForOneSYN(t *testing.T) {
	w := &latencyWatcher{now: time.Now, dial: func(ctx context.Context, network, address string) (net.Conn, error) {
		deadline, ok := ctx.Deadline()
		if !ok || time.Until(deadline) > latencyTCPTimeout {
			t.Errorf("dial allowed %v", time.Until(deadline))
		}
		<-ctx.Done()
		return nil, ctx.Err()
	}}
	p := &latencyProbe{w: w, t: latencyTarget{ID: "x", Host: "192.0.2.7", Port: 80, Mode: "tcp"}}
	start := time.Now()
	if _, ok := p.probe(context.Background(), latencyMaxTimeout); ok {
		t.Fatal("no answer within the SYN's time is a lost probe")
	}
	if took := time.Since(start); took > latencyTCPTimeout+500*time.Millisecond {
		t.Fatalf("took %v", took)
	}
}

// Minutes finished while no session takes them wait and go, oldest first, with the next one; the
// same targets given again keep their probes running.
func TestLatencyMinutesWaitForTheNextSession(t *testing.T) {
	var mu sync.Mutex
	clock := time.Unix(1_800_000_000-1_800_000_000%60, 0)
	dials := 0
	w := &latencyWatcher{
		now: func() time.Time { mu.Lock(); defer mu.Unlock(); return clock },
		dial: func(context.Context, string, string) (net.Conn, error) {
			mu.Lock()
			defer mu.Unlock()
			dials++
			clock = clock.Add(30 * time.Second)
			return nil, errors.New("unreachable")
		},
	}
	spec := latencyWatch{Interval: 3 * time.Millisecond, Targets: []latencyTarget{{ID: "x", Host: "192.0.2.7", Port: 80, Mode: "tcp"}}}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	tried := make(chan int, 64)
	w.watch(ctx, spec, func(m []latencyMinute) error { tried <- len(m); return errors.New("session gone") })
	<-tried
	// Cut off for a while: several minutes queue up.
	deadline := time.Now().Add(5 * time.Second)
	for {
		w.mu.Lock()
		queued := len(w.queue)
		w.mu.Unlock()
		if queued >= 4 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("queued %d", queued)
		}
		time.Sleep(5 * time.Millisecond)
	}
	mu.Lock()
	before := dials
	mu.Unlock()
	got := make(chan []latencyMinute, 64)
	w.watch(ctx, spec, func(m []latencyMinute) error { got <- m; return nil })
	first := <-got
	if len(first) < 4 {
		t.Fatalf("delivered %d", len(first))
	}
	for i, m := range first {
		if m.Target != "x" || m.Lost != m.Sent || m.Avg != nil || i > 0 && m.TS <= first[i-1].TS {
			t.Fatalf("minute %d: %+v", i, m)
		}
	}
	mu.Lock()
	after := dials
	mu.Unlock()
	if after < before {
		t.Fatal("probes restarted")
	}
	// Other targets replace these, and what was queued for them goes.
	w.watch(ctx, latencyWatch{Interval: time.Hour, Targets: []latencyTarget{{ID: "y", Host: "192.0.2.8", Port: 80, Mode: "tcp"}}}, func([]latencyMinute) error { return nil })
	w.mu.Lock()
	for _, q := range w.queue {
		if q.m.Target != "y" {
			t.Errorf("kept %+v", q.m)
		}
	}
	w.mu.Unlock()
}

func TestLatencyQueueKeepsTheNewest(t *testing.T) {
	w := &latencyWatcher{wake: make(chan struct{}, 1)}
	for i := range latencyQueueMax + 10 {
		w.finished(latencyMinute{TS: int64(i) * 60, Target: "x", Sent: 1, Lost: 1})
	}
	if len(w.queue) != latencyQueueMax || w.queue[0].m.TS != 600 {
		t.Fatalf("queue %d from %d", len(w.queue), w.queue[0].m.TS)
	}
}

// A target that takes the TCP connection but never answers TLS: each probe is lost at its own
// timeout, and the transport, which dials apart from the request, gives the handshake up too
// rather than keeping one connection per probe open.
func TestLatencyHTTPSHandshakesThatNeverEndAreGivenUp(t *testing.T) {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	var open atomic.Int32
	go func() {
		for {
			c, err := l.Accept()
			if err != nil {
				return
			}
			open.Add(1)
			go func() { io.Copy(io.Discard, c); open.Add(-1); c.Close() }()
		}
	}()
	raw := "https://" + l.Addr().String() + "/"
	p := &latencyProbe{w: newLatencyWatcher(), t: latencyTarget{ID: "web", Host: "127.0.0.1", Mode: "http", URL: raw}}
	defer p.close()
	for range 3 {
		if _, ok := p.probe(context.Background(), 20*time.Millisecond); ok {
			t.Fatal("no TLS answer: lost")
		}
	}
	deadline := time.Now().Add(latencyHTTPTimeout + 3*time.Second)
	for open.Load() > 0 && time.Now().Before(deadline) {
		time.Sleep(50 * time.Millisecond)
	}
	if n := open.Load(); n > 0 {
		t.Fatalf("%d handshakes still open after the timeout", n)
	}
}
