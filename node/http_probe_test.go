package node

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"syscall"
	"testing"
)

func probeOnce(t *testing.T, a *Agent, params any) ([]httpProbeResult, error) {
	t.Helper()
	raw, _ := json.Marshal(params)
	out, err := a.netHTTP(context.Background(), raw)
	if err != nil {
		return nil, err
	}
	return out.(map[string]any)["results"].([]httpProbeResult), nil
}

func TestNetHTTPFindsWhatTheControllerAsksFor(t *testing.T) {
	var mu sync.Mutex
	var got http.Header
	var posted string
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/moved":
			http.Redirect(w, r, "/sg-en/title/42", http.StatusFound)
		case "/sg-en/title/42":
			w.Write([]byte(`<html>"en-US" "zh-TW" "en-SG" Ad-Free Music</html>`))
		case "/api":
			body, _ := io.ReadAll(r.Body)
			mu.Lock()
			got, posted = r.Header.Clone(), string(body)
			mu.Unlock()
			w.Write([]byte(`{"body":{"appProps":{"region":"JP"}},"list":[{"n":7}]}`))
		default:
			w.WriteHeader(http.StatusForbidden)
			w.Write([]byte("Just a moment..."))
		}
	}))
	defer srv.Close()
	a := &Agent{}
	// Loopback is refused for real; here it is the test server.
	results, err := probeOnce(t, a, map[string]any{"family": 4, "requests": []any{map[string]any{"id": "x", "url": srv.URL + "/moved"}}})
	if err != nil || results[0].Error != "not_public" || results[0].Status != 0 {
		t.Fatalf("loopback dialled: %+v %v", results, err)
	}
	oldDial, oldTLS := httpProbeDial, httpProbeTLS
	defer func() { httpProbeDial, httpProbeTLS = oldDial, oldTLS }()
	httpProbeDial = func(string, string, syscall.RawConn) error { return nil }
	httpProbeTLS = srv.Client().Transport.(*http.Transport).TLSClientConfig
	results, err = probeOnce(t, a, map[string]any{"family": 4, "requests": []any{
		map[string]any{"id": "page", "url": srv.URL + "/moved", "find": []any{map[string]any{"re": `"[a-z]{2}-([A-Z]{2})"`}, map[string]any{"re": `"[a-z]{2}-([A-Z]{2})"`, "last": true}, map[string]any{"re": `nothing(\d)`}},
			"contains": []string{"ad-free", "not available"}},
		map[string]any{"id": "api", "url": srv.URL + "/api", "method": "POST", "body": `{"q":1}`,
			"headers": map[string]string{"X-Key": "k", "Accept-Encoding": "br"}, "json": []string{"body.appProps.region", "list.0.n", "list.3.n", "body"}},
		map[string]any{"id": "cf", "url": srv.URL + "/other", "contains": []string{"just a moment"}},
	}})
	if err != nil {
		t.Fatal(err)
	}
	page, api, cf := results[0], results[1], results[2]
	mu.Lock()
	defer mu.Unlock()
	if page.Status != 200 || !strings.HasSuffix(page.URL, "/sg-en/title/42") || page.Size == 0 {
		t.Fatalf("redirect not followed: %+v", page)
	}
	if strings.Join(page.Find, ",") != "US,SG," || page.Contains[0] != true || page.Contains[1] != false {
		t.Fatalf("findings %+v", page)
	}
	if strings.Join(api.JSON, ",") != "JP,7,," || posted != `{"q":1}` || got.Get("X-Key") != "k" || got.Get("Accept-Encoding") == "br" {
		t.Fatalf("api %+v posted %q headers %v", api, posted, got)
	}
	if cf.Status != 403 || !cf.Contains[0] {
		t.Fatalf("refusal %+v", cf)
	}
}

func TestNetHTTPRefusesWhatItWillNotDo(t *testing.T) {
	a := &Agent{}
	many := make([]any, httpProbeMaxRequests+1)
	for i := range many {
		many[i] = map[string]any{"id": "x", "url": "https://example.com/"}
	}
	for _, params := range []any{
		map[string]any{"family": 5, "requests": []any{map[string]any{"url": "https://example.com/"}}},
		map[string]any{"family": 4, "requests": []any{}},
		map[string]any{"family": 4, "requests": many},
		map[string]any{"family": 4, "requests": []any{map[string]any{"url": "http://example.com/"}}},
		map[string]any{"family": 4, "requests": []any{map[string]any{"url": "https://example.com/", "method": "DELETE"}}},
		map[string]any{"family": 4, "requests": []any{map[string]any{"url": "https://example.com/", "find": []any{map[string]any{"re": "("}}}}},
	} {
		if _, err := probeOnce(t, a, params); err == nil || err.Error() != "invalid_params" {
			t.Fatalf("%v accepted: %v", params, err)
		}
	}
	for _, addr := range []string{"127.0.0.1:443", "10.1.2.3:443", "192.168.1.1:443", "100.64.0.9:443", "169.254.1.1:443", "[::1]:443", "[fd00::1]:443", "[fe80::1]:443"} {
		if publicDestination("tcp", addr, nil) == nil {
			t.Fatalf("%s allowed", addr)
		}
	}
	for _, addr := range []string{"8.8.8.8:443", "[2606:4700::1111]:443"} {
		if publicDestination("tcp", addr, nil) != nil {
			t.Fatalf("%s refused", addr)
		}
	}
}
