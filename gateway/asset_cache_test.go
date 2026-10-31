package gateway

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
)

func TestImmutableAssetPaths(t *testing.T) {
	for _, path := range []string{"/_i/a/assets/index-CtVmYnDX.js", "/_i/a/assets/index-BOPpv-n9.css", "/_i/family_b-2/assets/editor.worker-DSkdb6T8.js", "/_i/a/assets/brand/openbear-d32cdfb09c17.png", "/_i/a/assets/KaTeX_Main-Regular-B22Nviop.woff2"} {
		for _, method := range []string{"GET", "HEAD"} {
			if !immutableAsset(httptest.NewRequest(method, path, nil)) {
				t.Errorf("compiled asset not cacheable: %s %s", method, path)
			}
		}
	}
	// Unprefixed assets come from nodes without instance prefixes and would share
	// one cache entry across instances.
	for _, path := range []string{"/skills", "/assets/index-CtVmYnDX.js", "/assets/brand/openbear-d32cdfb09c17.png", "/_i/a/assets/app.js", "/_i/a/assets/app-test.js", "/_i/a/assets/licenses/lucide.txt", "/_i/a/assets/../api/file-12345678.js", "/_i/a/assets-extra/index-CtVmYnDX.js", "/_i/a/api/files/index-CtVmYnDX.js", "/_i/a.b/assets/index-CtVmYnDX.js", "/_i//assets/index-CtVmYnDX.js", "/_i/assets/index-CtVmYnDX.js", "/api/files/index-CtVmYnDX.js", "/openbear-push-sw.js", "/manifest.webmanifest"} {
		if immutableAsset(httptest.NewRequest("GET", path, nil)) {
			t.Errorf("non-hashed build resource is cacheable: %s", path)
		}
	}
	if immutableAsset(httptest.NewRequest("POST", "/_i/a/assets/index-CtVmYnDX.js", nil)) {
		t.Fatal("write request is cacheable")
	}
}

func TestHashedAssetCacheValidationAndAuthentication(t *testing.T) {
	const etag = `"compiled-content"`
	const modified = "Mon, 12 Oct 2026 00:00:00 GMT"
	const policy = "private, max-age=31536000, immutable"
	var mu sync.Mutex
	var paths []string
	f := newFixture(t, func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		paths = append(paths, r.URL.Path)
		mu.Unlock()
		w.Header().Set("ETag", etag)
		w.Header().Set("Last-Modified", modified)
		w.Header().Set("Content-Type", "application/javascript")
		w.Header().Set("Cache-Control", "public, max-age=600")
		w.Header().Set("CDN-Cache-Control", "max-age=600")
		w.Header().Set("Surrogate-Control", "max-age=600")
		if r.URL.Query().Get("missing") != "" {
			w.WriteHeader(404)
			return
		}
		if r.Header.Get("If-None-Match") == etag || r.Header.Get("If-Modified-Since") == modified {
			w.WriteHeader(304)
			return
		}
		if r.Header.Get("Range") != "" {
			w.Header().Set("Content-Range", "bytes 0-3/10")
			w.WriteHeader(206)
			fmt.Fprint(w, "part")
			return
		}
		fmt.Fprint(w, "// compiled asset")
	})
	a := f.login(t, "alice", f.password)
	path := "/_i/a/assets/index-CtVmYnDX.js"
	headers := map[string]string{"Cookie": a.cookie.String()}
	for _, method := range []string{"GET", "HEAD"} {
		res, body := request(t, method, f.public.URL+path, nil, headers)
		requireStatus(t, res, body, 200)
		if res.Header.Get("Cache-Control") != policy || res.Header.Get("ETag") != etag || res.Header.Get("Last-Modified") != modified || res.Header.Get("CDN-Cache-Control") != "" || res.Header.Get("Surrogate-Control") != "" {
			t.Fatal("asset browser cache policy or validators lost")
		}
		if method == "HEAD" && len(body) != 0 {
			t.Fatal("HEAD returned a body")
		}
	}
	for key, value := range map[string]string{"If-None-Match": etag, "If-Modified-Since": modified} {
		h := map[string]string{"Cookie": a.cookie.String(), key: value}
		res, body := request(t, "GET", f.public.URL+path, nil, h)
		requireStatus(t, res, body, 304)
		if len(body) != 0 || res.Header.Get("Cache-Control") != policy {
			t.Fatal("asset revalidation did not preserve cache policy")
		}
		for _, fixed := range []string{"/skills", "/assets/shared.js", "/assets/index-CtVmYnDX.js", "/_i/a/assets/shared.js", "/manifest.webmanifest", "/openbear-push-sw.js"} {
			res, body = request(t, "GET", f.public.URL+fixed, nil, h)
			requireStatus(t, res, body, 200)
			if res.Header.Get("Cache-Control") != "no-store" {
				t.Fatalf("non-hashed response cached: %s", fixed)
			}
		}
	}
	mu.Lock()
	for _, got := range paths {
		if strings.HasPrefix(got, "/_i/") {
			t.Fatalf("instance prefix reached the node: %s", got)
		}
	}
	mu.Unlock()
	res, body := request(t, "GET", f.public.URL+path, nil, map[string]string{"Cookie": a.cookie.String(), "Range": "bytes=0-3"})
	requireStatus(t, res, body, 206)
	if string(body) != "part" || res.Header.Get("Cache-Control") != policy {
		t.Fatal("asset Range response changed")
	}
	res, body = request(t, "GET", f.public.URL+path+"?missing=1", nil, headers)
	requireStatus(t, res, body, 404)
	if res.Header.Get("Cache-Control") != "no-store" {
		t.Fatal("missing asset cached")
	}
	res, body = request(t, "GET", f.public.URL+"/api/data", nil, a.headers())
	requireStatus(t, res, body, 200)
	if res.Header.Get("Cache-Control") != "no-store" {
		t.Fatal("business response cached")
	}
	before := f.hits.Load()
	res, body = request(t, "GET", f.public.URL+path, nil, map[string]string{"If-None-Match": etag})
	requireStatus(t, res, body, 401)
	res, body = request(t, "GET", f.public.URL+path, nil, map[string]string{"Cookie": a.cookie.String(), "X-OpenBear-Context": "old-document", "If-None-Match": etag})
	requireStatus(t, res, body, 409)
	// Another instance's prefix never reaches this session's node.
	res, body = request(t, "GET", f.public.URL+"/_i/b/assets/index-CtVmYnDX.js", nil, headers)
	requireStatus(t, res, body, 409)
	if f.hits.Load() != before {
		t.Fatal("cache validation bypassed authentication")
	}
}
