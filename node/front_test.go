package node

import (
	"context"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// exitFor serves a front access on a free port that forwards to upstream, the exit's own address.
func exitFor(t *testing.T, upstream string) (*Agent, string, string) {
	t.Helper()
	exit := NewAgent(Config{URL: upstream}, log.New(io.Discard, "", 0))
	exit.ConfigPath = filepath.Join(t.TempDir(), "config.json")
	port := freePort(t)
	if err := exit.serveFront(port); err != nil {
		t.Fatalf("serve: %v %v", err, exit.frontStatus())
	}
	t.Cleanup(func() { exit.serveFront(0) })
	status := exit.frontStatus()
	return exit, "https://127.0.0.1:" + strconv.Itoa(port), status["fingerprint"].(string)
}

func TestFrontReachesTheControllerThroughItsExitByPin(t *testing.T) {
	f := newFake()
	hellos := make(chan map[string]any, 2)
	upstream := f.serve(t, func(ch *channel, _ map[string]any) {
		hellos <- next(t, ch, "hello")
		ch.conn.Read()
	})
	defer upstream.Close()
	_, access, fingerprint := exitFor(t, upstream.URL)

	front := f.agent(access, Capabilities{})
	front.Config.Pins = map[string][]string{access: {fingerprint}}
	front.Config.Front = true
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	go front.Run(ctx)
	select {
	case hello := <-hellos:
		if hello["front"] != true || hello["endpoint"] != access {
			t.Fatalf("hello: %v", hello)
		}
	case <-ctx.Done():
		t.Fatal("the front never reached the controller through its exit")
	}
}

func TestFrontRefusesAnExitWithAnotherCertificate(t *testing.T) {
	f := newFake()
	var sessions atomic.Int32
	upstream := f.serve(t, func(ch *channel, _ map[string]any) { sessions.Add(1) })
	defer upstream.Close()
	_, access, _ := exitFor(t, upstream.URL)
	front := f.agent(access, Capabilities{})
	front.Config.Pins = map[string][]string{access: {strings.Repeat("0", 64)}}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	front.Run(ctx)
	if sessions.Load() != 0 {
		t.Fatal("a certificate other than the pinned one was trusted")
	}
	// Without a pin the self-signed certificate is not trusted either.
	front = f.agent(access, Capabilities{})
	ctx, cancel = context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	front.Run(ctx)
	if sessions.Load() != 0 {
		t.Fatal("an unpinned self-signed certificate was trusted")
	}
}

func TestFrontAccessForwardsOnlyTheNodeAPIWithTheExitsHost(t *testing.T) {
	var hosts []string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hosts = append(hosts, r.Host)
		io.WriteString(w, "upstream "+r.URL.Path)
	}))
	defer upstream.Close()
	_, access, fingerprint := exitFor(t, upstream.URL)
	client := &http.Client{Transport: &http.Transport{TLSClientConfig: pinnedTLS([]string{fingerprint})}}
	get := func(path string) (int, string) {
		resp, err := client.Get(access + path)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		body, _ := io.ReadAll(resp.Body)
		return resp.StatusCode, string(body)
	}
	if code, body := get("/api/server-nodes/v1/hello"); code != 200 || body != "upstream /api/server-nodes/v1/hello" {
		t.Fatalf("node API: %d %q", code, body)
	}
	if code, _ := get("/admin"); code != http.StatusNotFound || len(hosts) != 1 {
		t.Fatalf("anything else must stay on the exit: %d, upstream saw %d", code, len(hosts))
	}
	if hosts[0] != strings.TrimPrefix(upstream.URL, "http://") {
		t.Fatalf("the controller must see its own host, not the access address: %q", hosts[0])
	}
}

func TestFrontCertificateIsKeptAcrossRestarts(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "front")
	_, first, err := frontCertificate(dir)
	if err != nil {
		t.Fatal(err)
	}
	cert, again, err := frontCertificate(dir)
	if err != nil || again != first {
		t.Fatalf("fingerprint changed: %s %s %v", first, again, err)
	}
	if len(cert.Certificate) != 1 {
		t.Fatal("unexpected certificate chain")
	}
}

func TestFrontServeIsSavedAndReported(t *testing.T) {
	f := newFake()
	port := freePort(t)
	replies := make(chan map[string]any, 1)
	hellos := make(chan map[string]any, 2)
	upstream := f.serve(t, func(ch *channel, _ map[string]any) {
		next(t, ch, "hello")
		replies <- call(t, ch, "front.serve", map[string]any{"port": port})
		hellos <- next(t, ch, "hello")
		ch.conn.Read()
	})
	defer upstream.Close()
	exit := f.agent(upstream.URL, Capabilities{Exec: true})
	exit.ConfigPath = filepath.Join(t.TempDir(), "config.json")
	SaveConfig(exit.ConfigPath, exit.Config)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	go exit.Run(ctx)
	defer exit.serveFront(0)
	reply := <-replies
	result, _ := reply["result"].(map[string]any)
	if reply["ok"] != true || result["listening"] != true || !validPin(result["fingerprint"].(string)) {
		t.Fatalf("front.serve: %v", reply)
	}
	access, _ := (<-hellos)["frontAccess"].(map[string]any)
	if access["fingerprint"] != result["fingerprint"] || access["port"] != float64(port) {
		t.Fatalf("hello must report the access: %v", access)
	}
	if saved, err := LoadConfig(exit.ConfigPath); err != nil || saved.FrontPort != port {
		t.Fatalf("port not saved: %v %v", saved.FrontPort, err)
	}
}

func TestTerminalDialsThroughTheNode(t *testing.T) {
	echo, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer echo.Close()
	go func() {
		for {
			c, err := echo.Accept()
			if err != nil {
				return
			}
			go func() { defer c.Close(); io.Copy(c, c) }()
		}
	}()
	f := newFake()
	done := make(chan string, 2)
	upstream := f.serve(t, func(ch *channel, _ map[string]any) {
		next(t, ch, "hello")
		ch.send(map[string]any{"t": "term.open", "sid": 7, "dial": "127.0.0.1:1"})
		done <- next(t, ch, "term.closed")["reason"].(string)
		ch.send(map[string]any{"t": "term.open", "sid": 8, "dial": echo.Addr().String()})
		next(t, ch, "term.opened")
		termInput(ch, 8, "ssh-2.0 hello\n")
		done <- output(t, ch, 8, "hello", map[string]map[string]any{})
		ch.conn.Read()
	})
	defer upstream.Close()
	// Commands may run here, the terminal may not: dialing needs the first.
	a := f.agent(upstream.URL, Capabilities{Exec: true})
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	go a.Run(ctx)
	if reason := <-done; reason != "connect_failed" {
		t.Fatalf("a refused dial: %q", reason)
	}
	if out := <-done; !strings.Contains(out, "ssh-2.0 hello") {
		t.Fatalf("tunnel output: %q", out)
	}
}

// The controller sends a tunnel's input without waiting for it to be taken: an exit whose link
// onwards is slow holds what SSH keeps in flight (its 2 MiB channel window) and closes nothing.
func TestTunnelHoldsInputPastASlowLink(t *testing.T) {
	keep := tunnelDial
	defer func() { tunnelDial = keep }()
	near, far := net.Pipe()
	defer far.Close()
	tunnelDial = func(context.Context, string) (console, error) { return tcpConsole{near}, nil }
	const sent = 3 << 20
	queued := make(chan struct{})
	f := newFake()
	upstream := f.serve(t, func(ch *channel, _ map[string]any) {
		next(t, ch, "hello")
		ch.send(map[string]any{"t": "term.open", "sid": 9, "dial": "198.51.100.1:22"})
		next(t, ch, "term.opened")
		chunk := strings.Repeat("x", 64<<10)
		for n := 0; n < sent; n += len(chunk) {
			termInput(ch, 9, chunk)
		}
		// The session reads in order: once this is answered every chunk has reached the tunnel.
		call(t, ch, "ping", nil)
		close(queued)
		ch.conn.Read()
	})
	defer upstream.Close()
	a := f.agent(upstream.URL, Capabilities{Exec: true})
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	go a.Run(ctx)
	select {
	case <-queued:
	case <-ctx.Done():
		t.Fatal("the input was not taken")
	}
	far.SetReadDeadline(time.Now().Add(10 * time.Second))
	if n, err := io.CopyN(io.Discard, far, sent); n != sent {
		t.Fatalf("the tunnel delivered %d of %d bytes: %v", n, sent, err)
	}
}

func TestTerminalDialNeedsExec(t *testing.T) {
	f := newFake()
	done := make(chan string, 1)
	upstream := f.serve(t, func(ch *channel, _ map[string]any) {
		next(t, ch, "hello")
		ch.send(map[string]any{"t": "term.open", "sid": 1, "dial": "127.0.0.1:22"})
		done <- next(t, ch, "term.closed")["reason"].(string)
		ch.conn.Read()
	})
	defer upstream.Close()
	a := f.agent(upstream.URL, Capabilities{Terminal: true})
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	go a.Run(ctx)
	if reason := <-done; reason != "capability_disabled" {
		t.Fatalf("dial without exec: %q", reason)
	}
}

func TestURLChangeMovesANodeOntoItsExitByPin(t *testing.T) {
	f := newFake()
	var fronts atomic.Int32
	moved := f.serve(t, func(ch *channel, auth map[string]any) {
		if auth["probe"] == true {
			return
		}
		if next(t, ch, "hello")["front"] == true {
			fronts.Add(1)
		}
		ch.conn.CloseWith(closeRevoked, "")
	})
	defer moved.Close()
	_, access, fingerprint := exitFor(t, moved.URL)
	replies := make(chan map[string]any, 1)
	old := f.serve(t, func(ch *channel, _ map[string]any) {
		replies <- call(t, ch, "config.url", map[string]any{"url": access, "pins": []string{fingerprint}, "front": true})
		ch.conn.Read()
	})
	defer old.Close()
	path := filepath.Join(t.TempDir(), "config.json")
	a := f.agent(old.URL, Capabilities{})
	a.ConfigPath = path
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	go func() {
		for fronts.Load() == 0 && ctx.Err() == nil {
			time.Sleep(20 * time.Millisecond)
		}
		cancel()
	}()
	a.Run(ctx)
	if reply := <-replies; reply["ok"] != true {
		t.Fatalf("config.url: %v", reply)
	}
	saved, err := LoadConfig(path)
	if err != nil || saved.URL != access || !saved.Front || len(saved.Pins[access]) != 1 || saved.Pins[access][0] != fingerprint {
		t.Fatalf("saved: %+v %v", saved, err)
	}
	if fronts.Load() != 1 {
		t.Fatal("the node did not reconnect as a front through its exit")
	}
}

func TestFrontAsksNoEchoService(t *testing.T) {
	var asked atomic.Int32
	echo := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { asked.Add(1); io.WriteString(w, "198.51.100.7") }))
	defer echo.Close()
	saved := [3][]string{publicV4Abroad, publicV4China, publicV6}
	publicV4Abroad, publicV4China, publicV6 = []string{echo.URL}, []string{echo.URL}, []string{echo.URL}
	defer func() { publicV4Abroad, publicV4China, publicV6 = saved[0], saved[1], saved[2] }()
	a := NewAgent(Config{Front: true}, log.New(io.Discard, "", 0))
	a.refreshIPs(context.Background(), nil, true)
	if asked.Load() != 0 || a.ipv4 != "" {
		t.Fatalf("a front asked %d echo services, ipv4 %q", asked.Load(), a.ipv4)
	}
	a = NewAgent(Config{}, log.New(io.Discard, "", 0))
	if v4, _, _ := a.publicIPs(context.Background()); v4 != "198.51.100.7" || asked.Load() == 0 {
		t.Fatalf("other nodes still look their address up: %q %d", v4, asked.Load())
	}
}

func TestFrontPortMoveKeepsOldListenerWhenNewPortIsBusy(t *testing.T) {
	f := newFake()
	a := f.agent("http://127.0.0.1:1", Capabilities{Exec: true})
	a.ConfigPath = filepath.Join(t.TempDir(), "config.json")
	old := freePort(t)
	if _, err := a.frontServe([]byte(`{"port":` + strconv.Itoa(old) + `}`)); err != nil {
		t.Fatal(err)
	}
	defer a.serveFront(0)
	before := a.frontStatus()
	busy, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer busy.Close()
	port := busy.Addr().(*net.TCPAddr).Port
	if _, err := a.frontServe([]byte(`{"port":` + strconv.Itoa(port) + `}`)); err == nil || err.Error() != "front_listen_failed" {
		t.Fatalf("busy port accepted: %v", err)
	}
	after := a.frontStatus()
	if after["port"] != old || after["listening"] != true || after["fingerprint"] != before["fingerprint"] || after["error"] == "" {
		t.Fatalf("lost old listener/status: %v", after)
	}
	if saved, err := LoadConfig(a.ConfigPath); err != nil || saved.FrontPort != old || a.config().FrontPort != old {
		t.Fatalf("failed port persisted: %v %v", saved, err)
	}
	transport := &http.Transport{TLSClientConfig: pinnedTLS([]string{before["fingerprint"].(string)}), DisableKeepAlives: true}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, Timeout: 3 * time.Second}
	response, err := client.Get("https://127.0.0.1:" + strconv.Itoa(old) + "/admin")
	if err != nil {
		t.Fatalf("old listener no longer accepts connections: %v", err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusNotFound {
		t.Fatal(response.Status)
	}
}

func TestPinsAreKeptOnlyForConfiguredHTTPSAddresses(t *testing.T) {
	pin := strings.Repeat("a", 64)
	c := Config{URL: "https://10.0.10.246:52002", Backup: "http://10.0.0.1:18961"}
	c.setPins([]string{pin}, []string{pin})
	if len(c.Pins) != 1 || c.Pins[c.URL][0] != pin {
		t.Fatalf("pins: %v", c.Pins)
	}
	if checkPins(c.Pins) != nil || checkPins(map[string][]string{"http://10.0.0.1": {pin}}) == nil {
		t.Fatal("pins must be for https addresses")
	}
	if _, err := cleanPins([]string{"xyz"}); err == nil {
		t.Fatal("a malformed pin was accepted")
	}
}
