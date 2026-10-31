package gateway

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
)

func TestClientIPNormalization(t *testing.T) {
	input := ClientIPConfig{Source: "header", Header: "x-fOrWaRdEd-For", TrustedProxies: []string{" 127.0.0.1 ", "127.0.0.1/032", "10.20.30.40/8", "2001:DB8::1/64", "::1", "::ffff:192.0.2.129/120"}}
	got, err := normalizeClientIP(input)
	if err != nil {
		t.Fatal(err)
	}
	want := ClientIPConfig{Source: "header", TrustedProxies: []string{"10.0.0.0/8", "127.0.0.1/32", "2001:db8::/64", "::1/128", "::ffff:c000:200/120"}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("normalization mismatch: %#v", got)
	}
	auto, err := normalizeClientIP(ClientIPConfig{Source: "auto", TrustedProxies: []string{"10.0.0.1"}})
	if err != nil || len(auto.TrustedProxies) != 0 {
		t.Fatal("auto policy must not keep manual proxies")
	}
	for _, entry := range []string{"", "host.example", "10.0.0.1/255.255.255.0", "127.0.0.1/33", "::/129", "fe80::1%eth0", "[::1]", "010.0.0.1", "127.0.0.1/+24", "127.0.0.1/-1", "127.0.0.1/２４"} {
		c := input
		c.TrustedProxies = []string{entry}
		if _, err := normalizeClientIP(c); err == nil {
			t.Errorf("invalid proxy accepted: %q", entry)
		}
	}
	for _, c := range []ClientIPConfig{{Source: "guess", TrustedProxies: []string{}}, {Source: "socket", Header: "X-Anything", TrustedProxies: []string{}}, {Source: "header", TrustedProxies: []string{}}, {Source: "auto"}, {Source: "header", TrustedProxies: make([]string, 129)}} {
		if _, err := normalizeClientIP(c); err == nil {
			t.Errorf("invalid policy accepted: %#v", c)
		}
	}
}

func gatewayWith(t *testing.T, c ClientIPConfig) *Gateway {
	t.Helper()
	normalized, err := normalizeClientIP(c)
	if err != nil {
		t.Fatal(err)
	}
	g := &Gateway{}
	g.ipPolicy.Store(compileClientIP(normalized))
	return g
}

func TestClientIPAutomaticDetection(t *testing.T) {
	auto := ClientIPConfig{Source: "auto", TrustedProxies: []string{}}
	for _, tc := range []struct {
		name, peer string
		headers    map[string][]string
		want       string
	}{
		{"direct-public", "203.0.113.1:123", nil, "203.0.113.1"},
		{"public-peer-spoof-ignored", "203.0.113.1:123", map[string][]string{"X-Forwarded-For": {"198.51.100.9"}, "X-Real-IP": {"198.51.100.9"}}, "203.0.113.1"},
		{"local-nginx-xff", "127.0.0.1:123", map[string][]string{"X-Forwarded-For": {"198.51.100.99, 203.0.113.9"}}, "203.0.113.9"},
		{"local-proxy-real-ip", "127.0.0.1:123", map[string][]string{"X-Real-IP": {"203.0.113.9"}}, "203.0.113.9"},
		{"private-proxy-needs-confirmation", "10.0.0.1:123", map[string][]string{"X-Real-IP": {"203.0.113.9"}}, "10.0.0.1"},
		{"private-peer-cf-header-ignored", "192.168.1.5:123", map[string][]string{"CF-Connecting-IP": {"203.0.113.9"}}, "192.168.1.5"},
		{"local-proxy-no-header", "127.0.0.1:123", nil, "127.0.0.1"},
		{"cloudflare-edge", "104.16.0.10:443", map[string][]string{"CF-Connecting-IP": {"203.0.113.50"}, "X-Forwarded-For": {"198.51.100.1, 104.16.0.10"}}, "203.0.113.50"},
		{"cloudflare-edge-v6", "[2606:4700::1]:443", map[string][]string{"CF-Connecting-IP": {"2001:db8::50"}}, "2001:db8::50"},
		{"cloudflare-edge-only-vouches-cf-header", "104.16.0.10:443", map[string][]string{"X-Forwarded-For": {"198.51.100.1"}, "True-Client-IP": {"198.51.100.2"}}, "104.16.0.10"},
		{"cf-header-from-non-cloudflare", "203.0.113.1:123", map[string][]string{"CF-Connecting-IP": {"198.51.100.50"}}, "203.0.113.1"},
		{"priority-real-ip-before-xff", "127.0.0.1:123", map[string][]string{"X-Real-IP": {"203.0.113.7"}, "X-Forwarded-For": {"198.51.100.1"}}, "203.0.113.7"},
		{"chain-of-local-hops", "127.0.0.1:123", map[string][]string{"X-Forwarded-For": {"203.0.113.9, 127.0.0.2, ::1"}}, "203.0.113.9"},
		{"lan-client-spoof-through-local-proxy", "127.0.0.1:123", map[string][]string{"X-Forwarded-For": {"203.0.113.9, 192.168.1.5"}}, "192.168.1.5"},
		{"lan-client-behind-local-proxy", "127.0.0.1:123", map[string][]string{"X-Forwarded-For": {"192.168.1.23"}}, "192.168.1.23"},
		{"bad-chain", "127.0.0.1:123", map[string][]string{"X-Forwarded-For": {"invalid, 203.0.113.9"}}, "127.0.0.1"},
		{"forwarded", "127.0.0.1:123", map[string][]string{"Forwarded": {`for=198.51.100.99;proto=http, for="203.0.113.9:1234", for=127.0.0.2`}}, "203.0.113.9"},
		{"forwarded-v6", "[::1]:123", map[string][]string{"Forwarded": {`for="[2001:db8:1::a]:456";proto=https`}}, "2001:db8:1::a"},
		{"forwarded-quoted-extension", "127.0.0.1:123", map[string][]string{"Forwarded": {`for=203.0.113.9;ext="one,two;three"`}}, "203.0.113.9"},
		{"forwarded-invalid-key", "127.0.0.1:123", map[string][]string{"Forwarded": {`for=203.0.113.9;bad key=yes`}}, "127.0.0.1"},
		{"forwarded-duplicate", "127.0.0.1:123", map[string][]string{"Forwarded": {`for=203.0.113.9;for=203.0.113.8`}}, "127.0.0.1"},
		{"forwarded-unknown", "127.0.0.1:123", map[string][]string{"Forwarded": {`for=unknown`}}, "127.0.0.1"},
		{"single-value-header-repeated", "127.0.0.1:123", map[string][]string{"X-Real-IP": {"203.0.113.9", "203.0.113.10"}}, "127.0.0.1"},
		{"zone-rejected", "127.0.0.1:123", map[string][]string{"X-Forwarded-For": {"fe80::1%eth0"}}, "127.0.0.1"},
		{"mapped-peer", "[::ffff:127.0.0.1]:123", map[string][]string{"X-Real-IP": {"::ffff:203.0.113.9"}}, "203.0.113.9"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			g := gatewayWith(t, auto)
			r := httptest.NewRequest("GET", "http://gateway.test/", nil)
			r.RemoteAddr = tc.peer
			for name, values := range tc.headers {
				for _, v := range values {
					r.Header.Add(name, v)
				}
			}
			if got := g.clientIP(r); got != tc.want {
				t.Fatalf("got %s want %s", got, tc.want)
			}
		})
	}
}

func TestClientIPManualAndSocketPolicies(t *testing.T) {
	r := httptest.NewRequest("GET", "http://gateway.test/", nil)
	r.RemoteAddr = "198.51.100.20:123"
	r.Header.Set("X-Forwarded-For", "203.0.113.9")
	if got := gatewayWith(t, ClientIPConfig{Source: "auto", TrustedProxies: []string{}}).clientIP(r); got != "198.51.100.20" {
		t.Fatal("public proxy trusted without configuration")
	}
	if got := gatewayWith(t, ClientIPConfig{Source: "header", TrustedProxies: []string{"198.51.100.0/24"}}).clientIP(r); got != "203.0.113.9" {
		t.Fatalf("manual proxy not trusted: %s", got)
	}
	// A confirmed LAN proxy is trusted, including hops through other listed proxies.
	lan := httptest.NewRequest("GET", "http://gateway.test/", nil)
	lan.RemoteAddr = "10.0.0.7:123"
	lan.Header.Set("X-Forwarded-For", "198.51.100.9, 203.0.113.9, 10.0.0.8")
	if got := gatewayWith(t, ClientIPConfig{Source: "header", TrustedProxies: []string{"10.0.0.0/24"}}).clientIP(lan); got != "203.0.113.9" {
		t.Fatalf("confirmed LAN proxy not trusted: %s", got)
	}
	if got := gatewayWith(t, ClientIPConfig{Source: "header", TrustedProxies: []string{"10.0.0.7"}}).clientIP(lan); got != "10.0.0.8" {
		t.Fatalf("unlisted hop trusted: %s", got)
	}
	edge := httptest.NewRequest("GET", "http://gateway.test/", nil)
	edge.RemoteAddr = "104.16.0.10:443"
	edge.Header.Set("CF-Connecting-IP", "203.0.113.50")
	if got := gatewayWith(t, ClientIPConfig{Source: "header", TrustedProxies: []string{"10.0.0.7"}}).clientIP(edge); got != "203.0.113.50" {
		t.Fatalf("manual proxies dropped Cloudflare detection: %s", got)
	}
	if got := gatewayWith(t, ClientIPConfig{Source: "socket", TrustedProxies: []string{}}).clientIP(edge); got != "104.16.0.10" {
		t.Fatal("socket policy read Cloudflare's header")
	}
	local := httptest.NewRequest("GET", "http://gateway.test/", nil)
	local.RemoteAddr = "127.0.0.1:123"
	local.Header.Set("X-Forwarded-For", "203.0.113.9")
	if got := gatewayWith(t, ClientIPConfig{Source: "socket", TrustedProxies: []string{}}).clientIP(local); got != "127.0.0.1" {
		t.Fatal("socket policy read a header")
	}
}

func putIP(t *testing.T, f *fixture, c any, want int) map[string]any {
	t.Helper()
	res, b := request(t, "PUT", f.public.URL+"/control/v1/client-ip", c, map[string]string{"Authorization": "Bearer " + f.cfg.ControlKey})
	requireStatus(t, res, b, want)
	return object(t, b)
}

func TestClientIPDefaultObservationAndLogin(t *testing.T) {
	f := newFixture(t, nil)
	if !f.g.ipPolicy.Load().Config.equal(defaultClientIP()) {
		t.Fatal("new gateways must detect automatically")
	}
	headers := map[string]string{"X-Forwarded-For": "198.51.100.50, 203.0.113.7"}
	res, b := request(t, "POST", f.public.URL+"/api/auth/password/login", map[string]any{"username": "alice", "password": f.password}, headers)
	requireStatus(t, res, b, 200)
	cookie := res.Cookies()[0]
	f.g.mu.Lock()
	s := f.g.state.Sessions[digest(cookie.Value)]
	f.g.mu.Unlock()
	if s.IP != "203.0.113.7" {
		t.Fatalf("session IP differs from detected IP: %s", s.IP)
	}
	res, b = request(t, "POST", f.public.URL+"/api/auth/password/login", map[string]any{"username": "unknown", "password": randomToken()}, headers)
	requireStatus(t, res, b, 401)
	f.g.mu.Lock()
	_, rightBucket := f.g.limits.Entries["ip:203.0.113.7"]
	f.g.mu.Unlock()
	if !rightBucket {
		t.Fatal("limiter did not use detected IP")
	}
	res, b = request(t, "GET", f.public.URL+"/control/v1/status", nil, map[string]string{"Authorization": "Bearer " + f.cfg.ControlKey})
	requireStatus(t, res, b, 200)
	observed, _ := object(t, b)["clientIpObserved"].(map[string]any)
	if observed["peer"] != "127.0.0.1" || observed["header"] != "X-Forwarded-For" || observed["trusted"] != true {
		t.Fatalf("observation not reported: %s", b)
	}
}

func TestClientIPUntrustedProxyObservationAndConfirmation(t *testing.T) {
	f := newFixture(t, nil)
	lan := httptest.NewRequest("GET", "http://gateway.test/", nil)
	lan.RemoteAddr = "192.168.1.20:123"
	lan.Header.Set("X-Forwarded-For", "203.0.113.9")
	if got := f.g.clientIP(lan); got != "192.168.1.20" {
		t.Fatalf("unconfirmed LAN proxy chose the client IP: %s", got)
	}
	if o := f.g.observedProxy(); o == nil || o.Peer != "192.168.1.20" || o.Header != "X-Forwarded-For" || o.Trusted {
		t.Fatalf("untrusted proxy not reported for confirmation: %#v", o)
	}
	// Public peers sending headers are ordinary clients, not proxy candidates.
	public := httptest.NewRequest("GET", "http://gateway.test/", nil)
	public.RemoteAddr = "198.51.100.20:123"
	public.Header.Set("X-Forwarded-For", "203.0.113.9")
	f.g.clientIP(public)
	if f.g.observedProxy().Peer != "192.168.1.20" {
		t.Fatal("public peer reported as a proxy candidate")
	}
	ack := putIP(t, f, ClientIPConfig{Source: "header", TrustedProxies: []string{"192.168.1.20"}}, 200)
	if o, _ := ack["clientIpObserved"].(map[string]any); o["peer"] != "192.168.1.20" || o["trusted"] != true {
		t.Fatalf("confirmed proxy still reported untrusted: %v", ack)
	}
	if got := f.g.clientIP(lan); got != "203.0.113.9" {
		t.Fatalf("confirmed proxy not trusted: %s", got)
	}
	ack = putIP(t, f, defaultClientIP(), 200)
	if o, _ := ack["clientIpObserved"].(map[string]any); o["trusted"] != false {
		t.Fatalf("removed proxy still reported trusted: %v", ack)
	}
}

func TestClientIPPersistenceHotApply(t *testing.T) {
	f := newFixture(t, nil)
	a := f.login(t, "alice", f.password)
	desired := ClientIPConfig{Source: "header", TrustedProxies: []string{"198.51.100.0/24"}}
	normalized, _ := normalizeClientIP(desired)
	ack := putIP(t, f, desired, 200)
	expected, _ := json.Marshal(normalized)
	actual, _ := json.Marshal(ack["clientIp"])
	if !jsonEqual(expected, actual) {
		t.Fatalf("ACK does not match effective policy: %s", actual)
	}
	f.call(t, a, "GET", "/api/data", nil, 200) // Hot IP policy must not revoke sessions.
	restartPushFixture(t, f)
	if !f.g.ipPolicy.Load().Config.equal(normalized) {
		t.Fatal("policy did not survive restart")
	}
	f.call(t, a, "GET", "/api/data", nil, 200)
	for _, bad := range []any{map[string]any{"source": "header", "trustedProxies": []string{}}, map[string]any{"source": "auto"}, map[string]any{"source": "auto", "trustedProxies": []string{}, "extra": true}} {
		putIP(t, f, bad, 400)
	}
	res, b := request(t, "PUT", f.public.URL+"/control/v1/client-ip", desired, map[string]string{"Authorization": "Bearer " + f.key})
	requireStatus(t, res, b, 401)
}
func jsonEqual(a, b []byte) bool {
	var x, y any
	return json.Unmarshal(a, &x) == nil && json.Unmarshal(b, &y) == nil && reflect.DeepEqual(x, y)
}
func TestClientIPInitialConfigAndLegacyState(t *testing.T) {
	for _, explicit := range []bool{false, true} {
		t.Run(fmt.Sprint(explicit), func(t *testing.T) {
			dir := t.TempDir()
			if err := saveState(dir, emptyState()); err != nil {
				t.Fatal(err)
			}
			c := Config{PublicURL: "http://127.0.0.1:18962", ControlKey: randomToken(), StateDir: dir, WebRoot: filepath.Join(dir, "does-not-exist")}
			expected := defaultClientIP()
			if explicit {
				expected = ClientIPConfig{Source: "header", TrustedProxies: []string{"198.51.100.1/32"}}
				c.ClientIP = &expected
			}
			g, err := New(c)
			if err != nil {
				t.Fatal(err)
			}
			if !g.ipPolicy.Load().Config.equal(expected) {
				t.Fatal("initial policy wrong")
			}
			g.Close()
			opposite := defaultClientIP()
			if !explicit {
				opposite = ClientIPConfig{Source: "header", TrustedProxies: []string{"198.51.100.1/32"}}
			}
			c.ClientIP = &opposite
			g, err = New(c)
			if err != nil {
				t.Fatal(err)
			}
			defer g.Close()
			if !g.ipPolicy.Load().Config.equal(expected) {
				t.Fatal("config overwrote persisted policy")
			}
		})
	}
	// A state written by the previous release keeps its explicit header choice working.
	dir := t.TempDir()
	legacy := emptyState()
	legacy.ClientIP = &ClientIPConfig{Source: "header", Header: "X-Real-IP", TrustedProxies: []string{"198.51.100.1/32"}}
	if err := saveState(dir, legacy); err != nil {
		t.Fatal(err)
	}
	g, err := New(Config{PublicURL: "http://127.0.0.1:18962", ControlKey: randomToken(), StateDir: dir})
	if err != nil {
		t.Fatal(err)
	}
	defer g.Close()
	r := httptest.NewRequest("GET", "http://gateway.test/", nil)
	r.RemoteAddr = "198.51.100.1:1"
	r.Header.Set("X-Real-IP", "203.0.113.4")
	if got := g.clientIP(r); got != "203.0.113.4" {
		t.Fatalf("legacy policy broken: %s", got)
	}
}
func TestClientIPPersistenceFailureDoesNotACKOrApply(t *testing.T) {
	f := newFixture(t, nil)
	old := f.g.ipPolicy.Load().Config
	path := filepath.Join(f.cfg.StateDir, "state.json")
	if err := os.Rename(path, path+".saved"); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(path, 0700); err != nil {
		t.Fatal(err)
	}
	putIP(t, f, ClientIPConfig{Source: "header", TrustedProxies: []string{"198.51.100.1"}}, 503)
	if !f.g.ipPolicy.Load().Config.equal(old) {
		t.Fatal("unpersisted policy was applied")
	}
}
func TestClientIPConcurrentHotApply(t *testing.T) {
	f := newFixture(t, nil)
	var wg sync.WaitGroup
	for n := 0; n < 4; n++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 3000; j++ {
				r := httptest.NewRequest("GET", "http://gateway.test/", nil)
				r.RemoteAddr = "127.0.0.1:123"
				r.Header.Set("X-Forwarded-For", "203.0.113.7")
				got := f.g.clientIP(r)
				if got != "127.0.0.1" && got != "203.0.113.7" {
					t.Errorf("partial policy: %s", got)
					return
				}
			}
		}()
	}
	for n := 0; n < 12; n++ {
		c := ClientIPConfig{Source: "socket", TrustedProxies: []string{}}
		if n%2 == 0 {
			c = defaultClientIP()
		}
		putIP(t, f, c, 200)
	}
	wg.Wait()
}
func TestClientIPStripSelectedHeadersAtNode(t *testing.T) {
	f := newFixture(t, nil)
	a := f.login(t, "alice", f.password)
	h := a.headers()
	for _, name := range forwardingHeaders {
		h[name] = "203.0.113.7"
	}
	res, data := request(t, "GET", f.public.URL+"/api/data", nil, h)
	requireStatus(t, res, data, 200)
	var v struct {
		Headers http.Header `json:"headers"`
	}
	json.Unmarshal(data, &v)
	for name := range h {
		if strings.Contains(strings.ToLower(name), "ip") || name == "Forwarded" || name == "X-Forwarded-For" {
			if v.Headers.Get(name) != "" {
				t.Fatalf("unverified header forwarded: %s", name)
			}
		}
	}
}
