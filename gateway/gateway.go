package gateway

import (
	"context"
	"errors"
	"net"
	"net/http"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

type Gateway struct {
	cfg          Config
	mu           sync.Mutex
	state        state
	storageErr   error
	lock         *os.File
	ipPolicy     atomic.Pointer[clientIPPolicy]
	observed     atomic.Pointer[proxyObservation]
	publicOrigin string
	secureCookie bool
	transport    *http.Transport
	client       *http.Client
	ctx          context.Context
	cancel       context.CancelFunc
	wg           sync.WaitGroup
	closeOnce    sync.Once
	active       map[string]map[uint64]context.CancelFunc
	nextActive   uint64
	limits       limiter
	kdfSlots     chan struct{}
	dummy        passwordHash
	build        string // sha256 of the running program, reported in status
}

func New(c Config) (*Gateway, error) {
	if e := c.validate(); e != nil {
		return nil, e
	}
	s, lock, e := openState(c.StateDir)
	if e != nil {
		return nil, e
	}
	initial := s.ClientIP == nil
	policy := defaultClientIP()
	if s.ClientIP != nil {
		policy = *s.ClientIP
	} else if c.ClientIP != nil {
		policy = *c.ClientIP
	}
	policy, e = normalizeClientIP(policy)
	if e != nil {
		lock.Close()
		return nil, e
	}
	s.ClientIP = &policy
	if initial {
		if e = saveState(c.StateDir, s); e != nil {
			lock.Close()
			return nil, e
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	u, _ := parseURL(c.PublicURL, true)
	tr := &http.Transport{DialContext: (&net.Dialer{Timeout: 10 * time.Second, KeepAlive: 30 * time.Second}).DialContext, ForceAttemptHTTP2: true, MaxIdleConns: 128, MaxIdleConnsPerHost: 16, MaxConnsPerHost: 128, IdleConnTimeout: 90 * time.Second, TLSHandshakeTimeout: 10 * time.Second, ExpectContinueTimeout: time.Second, DisableCompression: true}
	g := &Gateway{cfg: c, state: s, lock: lock, publicOrigin: origin(u), secureCookie: u.Scheme == "https", transport: tr, ctx: ctx, cancel: cancel, active: map[string]map[uint64]context.CancelFunc{}, kdfSlots: make(chan struct{}, 4), dummy: hashPassword(randomToken()), build: executableBuild()}
	g.ipPolicy.Store(compileClientIP(policy))
	g.client = &http.Client{Transport: tr, Timeout: 10 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	return g, nil
}
func (g *Gateway) Close() {
	g.closeOnce.Do(func() {
		g.cancel()
		g.mu.Lock()
		g.storageErr = errors.New("gateway closed")
		g.cancelConnections("")
		g.mu.Unlock()
		g.wg.Wait()
		g.transport.CloseIdleConnections()
		g.lock.Close()
	})
}
func (g *Gateway) cancelConnections(key string) {
	if key != "" {
		for _, cancel := range g.active[key] {
			cancel()
		}
		delete(g.active, key)
		return
	}
	for key := range g.active {
		g.cancelConnections(key)
	}
}

// Registration and session validation share mu, so revocation cannot race a new stream.
func (g *Gateway) authenticated(w http.ResponseWriter, r *http.Request, requireContext bool) (*http.Request, session, instance, func(), bool) {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.storageErr != nil {
		fail(w, 503, "state_unavailable")
		return r, session{}, instance{}, func() {}, false
	}
	s, i, ok := g.lookupSession(r)
	if !ok {
		fail(w, 401, "authentication_required")
		return r, s, i, func() {}, false
	}
	token := r.Header.Get("X-OpenBear-Context")
	if token == "" {
		token = r.URL.Query().Get("_ob_ctx")
	}
	if (requireContext || hasPageContext(r)) && !equal(s.Context, token) {
		fail(w, 409, "session_context_changed")
		return r, s, i, func() {}, false
	}
	if time.Now().Unix()-s.LastSeenAt >= 60 {
		if e := g.transaction(func(st *state) {
			v := st.Sessions[s.TokenHash]
			v.LastSeenAt = time.Now().Unix()
			st.Sessions[s.TokenHash] = v
		}); e != nil {
			fail(w, 503, "state_unavailable")
			return r, s, i, func() {}, false
		}
	}
	ctx, cancel := context.WithDeadline(r.Context(), time.Unix(s.ExpiresAt, 0))
	stop := context.AfterFunc(g.ctx, cancel)
	g.nextActive++
	id := g.nextActive
	if g.active[s.TokenHash] == nil {
		g.active[s.TokenHash] = map[uint64]context.CancelFunc{}
	}
	g.active[s.TokenHash][id] = cancel
	cleanup := func() {
		stop()
		cancel()
		g.mu.Lock()
		defer g.mu.Unlock()
		delete(g.active[s.TokenHash], id)
		if len(g.active[s.TokenHash]) == 0 {
			delete(g.active, s.TokenHash)
		}
	}
	return r.WithContext(ctx), s, i, cleanup, true
}
func (g *Gateway) lookupSession(r *http.Request) (session, instance, bool) {
	var token string
	for _, c := range r.Cookies() {
		if c.Name == cookieName {
			if token != "" {
				return session{}, instance{}, false
			}
			token = c.Value
		}
	}
	if len(token) != 43 {
		return session{}, instance{}, false
	}
	s, ok := g.state.Sessions[digest(token)]
	if !ok || s.ExpiresAt <= time.Now().Unix() {
		return s, instance{}, false
	}
	i, ok := g.state.Instances[s.InstanceID]
	return s, i, ok && i.Enabled
}
func (g *Gateway) PublicHandler() http.Handler  { return http.HandlerFunc(g.servePublic) }
func (g *Gateway) ControlHandler() http.Handler { return http.HandlerFunc(g.serveControl) }
func (g *Gateway) servePublic(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("Referrer-Policy", "no-referrer")
	if !cleanPath(r.URL.Path) {
		fail(w, 400, "invalid_path")
		return
	}
	if serviceWorkerScript(r) {
		fail(w, 403, "route_forbidden")
		return
	}
	if strings.HasPrefix(r.URL.Path, "/control/") || r.URL.Path == "/api/gateway/probe" {
		// With its own (loopback) control listener the public port has no control
		// API at all. A single-port configuration shares it, authenticated by the
		// control key; it is never a browser route.
		if g.cfg.ControlListen != "" {
			fail(w, 404, "not_found")
			return
		}
	}
	if strings.HasPrefix(r.URL.Path, "/control/") {
		g.serveControl(w, r)
		return
	}
	if r.URL.Path == "/api/gateway/v1/session-status" {
		if r.Method != http.MethodGet {
			fail(w, http.StatusMethodNotAllowed, "method_not_allowed")
			return
		}
		g.sessionStatus(w, r)
		return
	}
	if r.URL.Path == "/api/gateway/probe" {
		if r.Method != "GET" {
			fail(w, 405, "method_not_allowed")
			return
		}
		if !g.controlAuthorized(r) {
			fail(w, 401, "authentication_required")
			return
		}
		g.probe(w, r, r.Header.Get("X-OpenBear-Probe-Instance"))
		return
	}
	if strings.HasPrefix(r.URL.Path, "/i/") {
		g.webhook(w, r)
		return
	}
	if !g.browserOrigin(w, r) {
		return
	}
	if strings.HasPrefix(r.URL.Path, "/api/auth/") {
		g.auth(w, r)
		return
	}
	if blockedPath(r.URL.Path) {
		fail(w, 403, "route_forbidden")
		return
	}
	if r.URL.Path == "/login" {
		g.serveLogin(w, r)
		return
	}
	if bootstrapPath(r) {
		g.serveBootstrap(w, r)
		return
	}
	r, s, i, done, ok := g.authenticated(w, r, true)
	if !ok {
		return
	}
	defer done()
	g.proxy(w, r, i, &s, "")
}
func blockedPath(p string) bool {
	if strings.HasPrefix(p, "/api/instances") {
		return true
	}
	for _, prefix := range []string{"/api/managed", "/api/instances", "/api/instance-gateway", "/api/instance-events", "/api/gateway", "/api/system/restart", "/api/system/update", "/api/system/upgrade", "/api/auth", "/control", "/webhook"} {
		if p == prefix || strings.HasPrefix(p, prefix+"/") {
			return true
		}
	}
	return false
}
func (g *Gateway) controlAuthorized(r *http.Request) bool {
	return equal(r.Header.Get("Authorization"), "Bearer "+g.cfg.ControlKey)
}
func (g *Gateway) kdfSlot(w http.ResponseWriter) bool {
	select {
	case g.kdfSlots <- struct{}{}:
		return true
	default:
		w.Header().Set("Retry-After", "2")
		fail(w, 429, "try_later")
		return false
	}
}
