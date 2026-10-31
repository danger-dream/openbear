package gateway

import (
	_ "embed"
	"html/template"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
)

//go:embed login.html
var loginHTML string
var loginTemplate = template.Must(template.New("login").Parse(loginHTML))

// A service worker controls every instance on this origin, so the gateway
// serves its own copy of web/public/openbear-push-sw.js instead of a node's.
//
//go:embed openbear-push-sw.js
var pushWorker []byte

const pushWorkerPath = "/openbear-push-sw.js"

// The browser marks every service worker script fetch with this header.
func serviceWorkerScript(r *http.Request) bool {
	return len(r.Header.Values("Service-Worker")) > 0 && r.URL.Path != pushWorkerPath
}

// Each instance page loads its build below /_i/<instance>/, so long-lived
// browser cache entries of one instance never answer another on this origin.
const assetPrefixHeader = "X-OpenBear-Gateway-Assets"

func assetPrefix(id string) string { return "/_i/" + id + "/" }

// instanceAsset splits /_i/<instance>/assets/... into the instance and the
// path of that file on the node.
func instanceAsset(p string) (string, string, bool) {
	rest, ok := strings.CutPrefix(p, "/_i/")
	if !ok {
		return "", "", false
	}
	id, path, ok := strings.Cut(rest, "/")
	if !ok || !validID(id) || !strings.HasPrefix(path, "assets/") {
		return "", "", false
	}
	return id, "/" + path, true
}

// Keep these in sync with app/managed/http.py:GATEWAY_APP_PAGES and
// app/web_console/auth_api.py:PWA_PUBLIC_FILES. No generic root/static fallback.
var pagePaths = map[string]bool{"/": true, "/chat": true, "/memory": true, "/secrets": true, "/docs": true, "/mcp": true, "/skills": true, "/settings": true, "/statistics": true, "/webhooks": true, "/cron": true}
var pwaPaths = map[string]bool{"/openbear-push-sw.js": true, "/manifest.webmanifest": true, "/icons/openbear-192.png": true, "/icons/openbear-512.png": true, "/icons/apple-touch-icon.png": true}

func bootstrapPath(r *http.Request) bool {
	if r.Method != "GET" && r.Method != "HEAD" || !cleanPath(r.URL.Path) {
		return false
	}
	for _, c := range r.URL.Path {
		if c <= 32 || c == 127 {
			return false
		}
	}
	_, _, prefixed := instanceAsset(r.URL.Path)
	return pagePaths[r.URL.Path] || pwaPaths[r.URL.Path] || strings.HasPrefix(r.URL.Path, "/assets/") || prefixed
}

var assetHash = regexp.MustCompile(`-[A-Za-z0-9_-]{8,}\.[A-Za-z0-9]+$`)

// Unprefixed /assets/ come from nodes that predate instance prefixes; their
// cache entries would be shared by every instance, so they are never cached.
func immutableAsset(r *http.Request) bool {
	_, path, prefixed := instanceAsset(r.URL.Path)
	return (r.Method == "GET" || r.Method == "HEAD") && cleanPath(r.URL.Path) &&
		prefixed && assetHash.MatchString(path[strings.LastIndex(path, "/")+1:])
}

func hasPageContext(r *http.Request) bool {
	if len(r.Header.Values("X-OpenBear-Context")) > 0 {
		return true
	}
	_, ok := r.URL.Query()["_ob_ctx"]
	return ok
}
func safeLoginNext(value string) string {
	if len(value) > 4096 || !strings.HasPrefix(value, "/") || strings.HasPrefix(value, "//") || strings.Contains(value, "\\") {
		return "/"
	}
	for _, c := range value {
		if c <= 32 || c == 127 {
			return "/"
		}
	}
	u, err := url.Parse(value)
	if err != nil || u.Scheme != "" || u.Host != "" || u.Opaque != "" || !cleanPath(u.Path) || !pagePaths[u.Path] {
		return "/"
	}
	u.RawQuery = withoutContext(u.RawQuery)
	return u.String()
}
func (g *Gateway) serveLogin(w http.ResponseWriter, r *http.Request) {
	if r.Method != "GET" && r.Method != "HEAD" {
		fail(w, 405, "method_not_allowed")
		return
	}
	nonce := randomToken()
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Content-Security-Policy", "default-src 'none'; script-src 'nonce-"+nonce+"'; style-src 'nonce-"+nonce+"'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
	if r.Method == "HEAD" {
		return
	}
	_ = loginTemplate.Execute(w, struct{ Next, Nonce string }{safeLoginNext(r.URL.Query().Get("next")), nonce})
}
func (g *Gateway) serveBootstrap(w http.ResponseWriter, r *http.Request) {
	if pagePaths[r.URL.Path] && !hasPageContext(r) {
		g.mu.Lock()
		_, _, valid := g.lookupSession(r)
		storageOK := g.storageErr == nil
		g.mu.Unlock()
		if storageOK && !valid {
			w.Header().Set("Cache-Control", "no-store")
			http.Redirect(w, r, "/login?next="+url.QueryEscape(safeLoginNext(r.URL.RequestURI())), http.StatusFound)
			return
		}
	}
	r, s, i, done, ok := g.authenticated(w, r, false)
	if !ok {
		return
	}
	defer done()
	if r.URL.Path == pushWorkerPath {
		w.Header().Set("Content-Type", "application/javascript")
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("Content-Length", strconv.Itoa(len(pushWorker)))
		if r.Method != "HEAD" {
			_, _ = w.Write(pushWorker)
		}
		return
	}
	upstreamPath := ""
	if id, path, prefixed := instanceAsset(r.URL.Path); prefixed {
		if !equal(id, i.ID) {
			fail(w, 409, "session_context_changed")
			return
		}
		upstreamPath = path
	}
	// Only an instance's own content-hashed assets may revalidate a cache entry.
	if !immutableAsset(r) {
		r = r.Clone(r.Context())
		r.Header.Del("If-None-Match")
		r.Header.Del("If-Modified-Since")
	}
	g.proxy(w, r, i, &s, upstreamPath)
}
