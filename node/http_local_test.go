package node

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func localPort(t *testing.T, s *httptest.Server) int {
	t.Helper()
	return s.Listener.Addr().(*net.TCPAddr).Port
}

func callLocal(t *testing.T, a *Agent, params map[string]any) (map[string]any, error) {
	t.Helper()
	raw, _ := json.Marshal(params)
	r, _, err := a.dispatch(context.Background(), envelope{Method: "http.local", Params: raw})
	if err != nil {
		return nil, err
	}
	return r.(map[string]any), nil
}

func TestHTTPLocalRelaysOneLoopbackRequest(t *testing.T) {
	var seen *http.Request
	var body string
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = r
		b, _ := io.ReadAll(r.Body)
		body = string(b)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(201)
		w.Write([]byte(`{"ok":true}`))
	}))
	defer s.Close()
	a := NewAgent(Config{Capabilities: Capabilities{Exec: true}}, log.New(io.Discard, "", 0))
	r, err := callLocal(t, a, map[string]any{"method": "PUT", "port": localPort(t, s), "path": "/control/v1/instances/a%2Fb?after=3",
		"headers": map[string]string{"Authorization": "Bearer k", "Content-Type": "application/json"},
		"body":    base64.StdEncoding.EncodeToString([]byte(`{"x":1}`))})
	if err != nil {
		t.Fatal(err)
	}
	got, _ := base64.StdEncoding.DecodeString(r["body"].(string))
	if r["status"] != 201 || string(got) != `{"ok":true}` || r["contentType"] != "application/json" {
		t.Fatalf("reply %v", r)
	}
	if seen.Method != "PUT" || seen.URL.RawQuery != "after=3" || seen.URL.EscapedPath() != "/control/v1/instances/a%2Fb" ||
		seen.Header.Get("Authorization") != "Bearer k" || body != `{"x":1}` {
		t.Fatalf("request %s %s %v %q", seen.Method, seen.URL, seen.Header, body)
	}
}

func TestHTTPLocalNeedsCommandConsentAndStaysOnLoopback(t *testing.T) {
	redirect := httptest.NewServer(http.RedirectHandler("http://192.0.2.1/", http.StatusFound))
	defer redirect.Close()
	off := NewAgent(Config{}, log.New(io.Discard, "", 0))
	if _, err := callLocal(t, off, map[string]any{"method": "GET", "port": localPort(t, redirect), "path": "/"}); err == nil || err.Error() != "capability_disabled" {
		t.Fatalf("exec off: %v", err)
	}
	a := NewAgent(Config{Capabilities: Capabilities{Exec: true}}, log.New(io.Discard, "", 0))
	// Redirects are returned, never followed off this machine.
	r, err := callLocal(t, a, map[string]any{"method": "GET", "port": localPort(t, redirect), "path": "/"})
	if err != nil || r["status"] != http.StatusFound {
		t.Fatalf("redirect: %v %v", r, err)
	}
	for _, p := range []map[string]any{
		{"method": "GET", "port": 0, "path": "/"},
		{"method": "CONNECT", "port": 80, "path": "/"},
		{"method": "GET", "port": 80, "path": "//192.0.2.1/x"},
		{"method": "GET", "port": 80, "path": "@192.0.2.1/x"},
		{"method": "GET", "port": 80, "path": "/a b"},
		{"method": "GET", "port": 80, "path": "/#x"},
		{"method": "GET", "port": 80, "path": "/", "headers": map[string]string{"Host": "example.com"}},
		{"method": "GET", "port": 80, "path": "/", "headers": map[string]string{"Authorization": "a\r\nX: y"}},
		{"method": "GET", "port": 80, "path": "/", "timeoutMs": 120000},
	} {
		if _, err := callLocal(t, a, p); err == nil || err.Error() != "invalid_params" {
			t.Errorf("%v: %v", p, err)
		}
	}
	closed := httptest.NewServer(http.NotFoundHandler())
	port := localPort(t, closed)
	closed.Close()
	if _, err := callLocal(t, a, map[string]any{"method": "GET", "port": port, "path": "/"}); err == nil || err.Error() != "connection_failed" {
		t.Fatalf("closed port: %v", err)
	}
}

func TestHTTPLocalBoundsTheResponse(t *testing.T) {
	large := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(strings.Repeat("x", httpLocalMaxBody+1)))
	}))
	defer large.Close()
	slow := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { <-r.Context().Done() }))
	defer slow.Close()
	a := NewAgent(Config{Capabilities: Capabilities{Exec: true}}, log.New(io.Discard, "", 0))
	if _, err := callLocal(t, a, map[string]any{"method": "GET", "port": localPort(t, large), "path": "/"}); err == nil || err.Error() != "response_too_large" {
		t.Fatalf("large: %v", err)
	}
	if _, err := callLocal(t, a, map[string]any{"method": "GET", "port": localPort(t, slow), "path": "/", "timeoutMs": 200}); err == nil || err.Error() != "timeout" {
		t.Fatalf("slow: %v", err)
	}
}
