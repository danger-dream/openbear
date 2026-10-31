package gateway

import (
	"net/http"
	"sort"
	"strconv"
	"strings"
	"time"
)

func (g *Gateway) auth(w http.ResponseWriter, r *http.Request) {
	p := r.URL.Path
	if p == "/api/auth/mode" && r.Method == "GET" {
		jsonResponse(w, 200, map[string]any{"ok": true, "mode": "gateway"})
		return
	}
	if p == "/api/auth/password/login" && r.Method == "POST" {
		g.login(w, r)
		return
	}
	known := (p == "/api/auth/session" && r.Method == "GET") || (p == "/api/auth/logout" && r.Method == "POST") || (p == "/api/auth/sessions" && r.Method == "GET") || (strings.HasPrefix(p, "/api/auth/sessions/") && r.Method == "DELETE") || (p == "/api/auth/password/change" && r.Method == "POST")
	if !known {
		fail(w, 404, "not_found")
		return
	}
	r, s, i, done, ok := g.authenticated(w, r, p != "/api/auth/session")
	if !ok {
		return
	}
	defer done()
	switch {
	case p == "/api/auth/session":
		jsonResponse(w, 200, map[string]any{"ok": true, "mode": "gateway", "chatId": 1, "instanceId": i.ID, "username": i.Username, "contextToken": s.Context, "expiresAt": s.ExpiresAt})
	case p == "/api/auth/logout":
		g.logout(w, r, s)
	case p == "/api/auth/sessions":
		g.sessions(w, s)
	case p == "/api/auth/password/change":
		g.changePassword(w, r, s, i)
	default:
		g.revoke(w, r, s, strings.TrimPrefix(p, "/api/auth/sessions/"))
	}
}
func (g *Gateway) login(w http.ResponseWriter, r *http.Request) {
	var p struct {
		Username string `json:"username"`
		Password string `json:"password"`
	}
	if !decodeBody(w, r, &p) {
		return
	}
	ip := g.clientIP(r) // One immutable policy snapshot for the whole attempt.
	p.Username = strings.TrimSpace(p.Username)
	if len(p.Username) > 128 || len(p.Password) > 1024 {
		fail(w, 401, "invalid_credentials")
		return
	}
	g.mu.Lock()
	if g.storageErr != nil {
		g.mu.Unlock()
		fail(w, 503, "state_unavailable")
		return
	}
	if _, _, ok := g.lookupSession(r); ok {
		g.mu.Unlock()
		fail(w, 409, "logout_required")
		return
	}
	if !g.limits.allow(ip, p.Username) {
		g.mu.Unlock()
		w.Header().Set("Retry-After", "30")
		fail(w, 429, "try_later")
		return
	}
	var account *instance
	for _, i := range g.state.Instances {
		// An archived instance may share the name; the enabled one is the account.
		if i.Username == p.Username && (account == nil || i.Enabled && !account.Enabled) {
			copy := i
			account = &copy
		}
	}
	h := g.dummy
	if account != nil {
		h = account.Password
	}
	g.mu.Unlock()
	if !g.kdfSlot(w) {
		return
	}
	defer func() { <-g.kdfSlots }()
	valid := h.verify(p.Password) && account != nil && account.Enabled
	g.mu.Lock()
	defer g.mu.Unlock()
	if account != nil {
		current, ok := g.state.Instances[account.ID]
		valid = valid && ok && current.Enabled && current.Revision == account.Revision && current.Username == account.Username && current.Password == account.Password
	}
	if !valid {
		g.limits.failure(ip, p.Username)
		if e := g.transaction(func(s *state) { addLoginEvent(s, account, false, ip) }); e != nil {
			fail(w, 503, "state_unavailable")
			return
		}
		fail(w, 401, "invalid_credentials")
		return
	}
	raw := randomToken()
	now := time.Now()
	sess := session{InstanceID: account.ID, TokenHash: digest(raw), Context: randomToken(), UpstreamID: randomToken(), CreatedAt: now.Unix(), ExpiresAt: now.Add(sessionLifetime).Unix(), LastSeenAt: now.Unix(), IP: ip, UserAgent: r.UserAgent()}
	if len(sess.UserAgent) > 1024 {
		sess.UserAgent = sess.UserAgent[:1024]
	}
	if e := g.transaction(func(s *state) {
		pruneSessions(s)
		var own []session
		for _, v := range s.Sessions {
			if v.InstanceID == account.ID {
				own = append(own, v)
			}
		}
		sort.Slice(own, func(a, b int) bool { return own[a].ID < own[b].ID })
		for len(own) >= 100 {
			delete(s.Sessions, own[0].TokenHash)
			own = own[1:]
		}
		if len(s.Sessions) >= maxSessions {
			var oldest session
			for _, v := range s.Sessions {
				if oldest.ID == 0 || v.ID < oldest.ID {
					oldest = v
				}
			}
			delete(s.Sessions, oldest.TokenHash)
		}
		s.NextSessionID++
		sess.ID = s.NextSessionID
		s.Sessions[sess.TokenHash] = sess
		addLoginEvent(s, account, true, ip)
	}); e != nil {
		fail(w, 503, "state_unavailable")
		return
	}
	g.limits.success(ip, p.Username)
	g.cookie(w, raw, time.Unix(sess.ExpiresAt, 0))
	jsonResponse(w, 200, map[string]any{"ok": true})
}
func (g *Gateway) logout(w http.ResponseWriter, r *http.Request, s session) {
	g.mu.Lock()
	defer g.mu.Unlock()
	if _, ok := g.state.Sessions[s.TokenHash]; !ok {
		fail(w, 401, "authentication_required")
		return
	}
	if e := g.transaction(func(st *state) { delete(st.Sessions, s.TokenHash) }); e != nil {
		fail(w, 503, "state_unavailable")
		return
	}
	g.cookie(w, "", time.Time{})
	jsonResponse(w, 200, map[string]any{"ok": true})
}
func (g *Gateway) sessions(w http.ResponseWriter, current session) {
	g.mu.Lock()
	defer g.mu.Unlock()
	found := []session{}
	for _, s := range g.state.Sessions {
		if s.InstanceID == current.InstanceID && s.ExpiresAt > time.Now().Unix() {
			found = append(found, s)
		}
	}
	sort.Slice(found, func(a, b int) bool {
		if found[a].ID == current.ID {
			return true
		}
		if found[b].ID == current.ID {
			return false
		}
		return found[a].ID > found[b].ID
	})
	items := make([]map[string]any, 0, len(found))
	for _, s := range found {
		items = append(items, map[string]any{"id": s.ID, "createdAt": s.CreatedAt, "expiresAt": s.ExpiresAt, "lastSeenAt": s.LastSeenAt, "ip": s.IP, "userAgent": s.UserAgent, "current": s.ID == current.ID})
	}
	jsonResponse(w, 200, map[string]any{"ok": true, "items": items, "total": len(items)})
}
func (g *Gateway) revoke(w http.ResponseWriter, r *http.Request, current session, idText string) {
	id, e := strconv.ParseInt(idText, 10, 64)
	if e != nil || id < 1 {
		fail(w, 404, "session_not_found")
		return
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	if _, ok := g.state.Sessions[current.TokenHash]; !ok {
		fail(w, 401, "authentication_required")
		return
	}
	key := ""
	for k, s := range g.state.Sessions {
		if s.InstanceID == current.InstanceID && s.ID == id && s.ExpiresAt > time.Now().Unix() {
			key = k
		}
	}
	if key == "" {
		fail(w, 404, "session_not_found")
		return
	}
	if e = g.transaction(func(s *state) { delete(s.Sessions, key) }); e != nil {
		fail(w, 503, "state_unavailable")
		return
	}
	if key == current.TokenHash {
		g.cookie(w, "", time.Time{})
	}
	jsonResponse(w, 200, map[string]any{"ok": true, "sessionId": id, "current": key == current.TokenHash})
}
func (g *Gateway) changePassword(w http.ResponseWriter, r *http.Request, s session, i instance) {
	ip := g.clientIP(r)
	var p struct {
		CurrentPassword string  `json:"currentPassword"`
		Password        string  `json:"password"`
		Username        *string `json:"username,omitempty"`
	}
	if !decodeBody(w, r, &p) {
		return
	}
	if !validPassword(p.Password) || (p.Username != nil && !validUsername(*p.Username)) {
		fail(w, 400, "invalid_account")
		return
	}
	g.mu.Lock()
	allowed := g.limits.allow(ip, i.Username)
	g.mu.Unlock()
	if !allowed {
		w.Header().Set("Retry-After", "30")
		fail(w, 429, "try_later")
		return
	}
	if !g.kdfSlot(w) {
		return
	}
	defer func() { <-g.kdfSlots }()
	if !i.Password.verify(p.CurrentPassword) {
		g.mu.Lock()
		g.limits.failure(ip, i.Username)
		g.mu.Unlock()
		fail(w, 401, "invalid_credentials")
		return
	}
	h := hashPassword(p.Password)
	g.mu.Lock()
	defer g.mu.Unlock()
	if _, ok := g.state.Sessions[s.TokenHash]; !ok {
		fail(w, 401, "authentication_required")
		return
	}
	current := g.state.Instances[i.ID]
	if current.Password != i.Password || current.Username != i.Username {
		fail(w, 409, "session_context_changed")
		return
	}
	if p.Username != nil {
		for id, v := range g.state.Instances {
			if id != i.ID && v.Username == *p.Username && v.Enabled {
				fail(w, 409, "username_unavailable")
				return
			}
		}
		current.Username = *p.Username
	}
	current.Password = h
	if e := g.transaction(func(st *state) { st.Instances[i.ID] = current; revokeInstance(st, i.ID) }); e != nil {
		fail(w, 503, "state_unavailable")
		return
	}
	g.limits.success(ip, i.Username)
	if current.Username != i.Username {
		g.limits.success(ip, current.Username)
	}
	g.cookie(w, "", time.Time{})
	jsonResponse(w, 200, map[string]any{"ok": true})
}
