package gateway

import (
	"encoding/json"
	"io"
	"net"
	"net/http"
	"sort"
	"strings"
)

type instanceInput struct {
	Revision    int64   `json:"revision"`
	Upstream    string  `json:"upstream"`
	BusinessKey string  `json:"businessKey"`
	Username    *string `json:"username,omitempty"`
	Password    *string `json:"password,omitempty"`
	Enabled     *bool   `json:"enabled"`
}

func summary(i instance) map[string]any {
	return map[string]any{"id": i.ID, "revision": i.Revision, "enabled": i.Enabled, "username": i.Username}
}
func (g *Gateway) serveControl(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	if !g.controlAuthorized(r) {
		fail(w, 401, "authentication_required")
		return
	}
	if !cleanPath(r.URL.Path) {
		fail(w, 400, "invalid_path")
		return
	}
	if r.URL.Path == "/control/v1/client-ip" && r.Method == "PUT" {
		g.putClientIP(w, r)
		return
	}
	if r.URL.Path == "/control/v1/events" && r.Method == "GET" {
		g.events(w, r)
		return
	}
	if r.URL.Path == "/control/v1/status" && r.Method == "GET" {
		g.mu.Lock()
		defer g.mu.Unlock()
		if g.storageErr != nil {
			fail(w, 503, "state_unavailable")
			return
		}
		ids := make([]string, 0, len(g.state.Instances))
		for id := range g.state.Instances {
			ids = append(ids, id)
		}
		sort.Strings(ids)
		items := make([]map[string]any, 0, len(ids))
		for _, id := range ids {
			items = append(items, summary(g.state.Instances[id]))
		}
		jsonResponse(w, 200, map[string]any{"ok": true, "version": 1, "build": g.build, "publicUrl": g.cfg.PublicURL, "clientIp": g.ipPolicy.Load().Config, "clientIpObserved": g.observedProxy(), "instances": items, "eventsPending": len(g.state.Events), "eventsDropped": g.state.DroppedEvents})
		return
	}
	parts := strings.Split(strings.TrimPrefix(r.URL.Path, "/control/v1/instances/"), "/")
	if !strings.HasPrefix(r.URL.Path, "/control/v1/instances/") || len(parts) < 1 || !validID(parts[0]) {
		fail(w, 404, "not_found")
		return
	}
	if len(parts) == 2 && parts[1] == "probe" && r.Method == "GET" {
		g.probe(w, r, parts[0])
		return
	}
	if len(parts) == 1 && r.Method == "PUT" {
		g.putInstance(w, r, parts[0])
		return
	}
	fail(w, 404, "not_found")
}
func (g *Gateway) validateInput(p *instanceInput) bool {
	if p.Revision < 1 || p.Enabled == nil || !validSecret(p.BusinessKey) || equal(p.BusinessKey, g.cfg.ControlKey) {
		return false
	}
	if p.Username != nil && !validUsername(*p.Username) || p.Password != nil && !validPassword(*p.Password) {
		return false
	}
	u, e := parseURL(p.Upstream, true)
	if e != nil || origin(u) == g.publicOrigin {
		return false
	}
	for _, addr := range []string{g.cfg.Listen, g.cfg.ControlListen} {
		h, port, e := net.SplitHostPort(addr)
		if e == nil && u.Host == net.JoinHostPort(h, port) {
			return false
		}
	}
	p.Upstream = u.String()
	return true
}
func configDigest(p instanceInput) string {
	p.Password = nil
	b, _ := json.Marshal(p)
	return digest(string(b))
}
func (g *Gateway) putInstance(w http.ResponseWriter, r *http.Request, id string) {
	var p instanceInput
	if !decodeBody(w, r, &p) {
		return
	}
	if !g.validateInput(&p) {
		fail(w, 400, "invalid_instance_config")
		return
	}
	if !g.kdfSlot(w) {
		return
	}
	defer func() { <-g.kdfSlots }()
	// Control updates are serialized with authentication commits. Password work is
	// bounded and only on authenticated management requests.
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.storageErr != nil {
		fail(w, 503, "state_unavailable")
		return
	}
	old, exists := g.state.Instances[id]
	fp := configDigest(p)
	if exists && p.Revision <= old.Revision {
		same := p.Revision == old.Revision && equal(fp, old.ConfigDigest)
		if same {
			same = (p.Password == nil && old.ConfigPassword == nil) || (p.Password != nil && old.ConfigPassword != nil && old.ConfigPassword.verify(*p.Password))
		}
		if !same {
			fail(w, 409, "revision_conflict")
			return
		}
		v := summary(old)
		v["ok"] = true
		jsonResponse(w, 200, v)
		return
	}
	if !exists && (p.Username == nil || p.Password == nil) {
		fail(w, 400, "initial_account_required")
		return
	}
	if !exists && len(g.state.Instances) >= 1000 {
		fail(w, 409, "instance_limit")
		return
	}
	next := old
	next.ID = id
	next.Revision = p.Revision
	next.Upstream = p.Upstream
	next.BusinessKey = p.BusinessKey
	next.Enabled = *p.Enabled
	next.ConfigDigest = fp
	next.ConfigPassword = nil
	if p.Username != nil {
		next.Username = *p.Username
	}
	// An archived (disabled) instance keeps its name but does not hold it against a new one.
	for other, i := range g.state.Instances {
		if other != id && i.Username == next.Username && i.Enabled && next.Enabled {
			fail(w, 409, "username_unavailable")
			return
		}
	}
	if p.Password != nil {
		h := hashPassword(*p.Password)
		next.Password = h
		next.ConfigPassword = &h
	}
	revoke := !exists || next.Upstream != old.Upstream || next.BusinessKey != old.BusinessKey || next.Username != old.Username || next.Enabled != old.Enabled || p.Password != nil
	if e := g.transaction(func(s *state) {
		s.Instances[id] = next
		if revoke {
			revokeInstance(s, id)
		}
	}); e != nil {
		fail(w, 503, "state_unavailable")
		return
	}
	v := summary(next)
	v["ok"] = true
	jsonResponse(w, 200, v)
}
func (g *Gateway) probe(w http.ResponseWriter, r *http.Request, id string) {
	g.mu.Lock()
	i, exists := g.state.Instances[id]
	bad := g.storageErr != nil
	g.mu.Unlock()
	if bad {
		fail(w, 503, "state_unavailable")
		return
	}
	if !exists {
		fail(w, 404, "instance_not_found")
		return
	}
	result := map[string]any{"ok": true, "id": id, "revision": i.Revision, "reachable": false, "status": nil}
	req, e := http.NewRequestWithContext(r.Context(), "GET", i.Upstream+"/api/gateway/v1/probe", nil)
	if e != nil {
		result["error"] = "invalid_upstream"
		jsonResponse(w, 200, result)
		return
	}
	req.Header.Set("X-OpenBear-Gateway-Key", i.BusinessKey)
	req.Header.Set("X-OpenBear-Instance", i.ID)
	res, e := g.client.Do(req)
	if e != nil {
		result["error"] = "upstream_unreachable"
		jsonResponse(w, 200, result)
		return
	}
	defer res.Body.Close()
	result["httpStatus"] = res.StatusCode
	if res.StatusCode != 200 {
		result["error"] = "upstream_rejected"
		jsonResponse(w, 200, result)
		return
	}
	var status map[string]any
	b, e := io.ReadAll(io.LimitReader(res.Body, 1<<20+1))
	if e != nil || len(b) > 1<<20 || json.Unmarshal(b, &status) != nil || status == nil {
		result["error"] = "invalid_probe"
		jsonResponse(w, 200, result)
		return
	}
	// Return the actual downstream identity and health, never manufacture a match.
	safe := map[string]any{}
	for _, key := range []string{"ok", "instanceId", "role", "mode", "authMode", "ready", "health", "persistence", "version", "status"} {
		if v, ok := status[key]; ok {
			safe[key] = v
		}
	}
	result["status"] = safe
	if status["instanceId"] != id {
		result["error"] = "instance_identity_mismatch"
	} else {
		result["reachable"] = true
	}
	jsonResponse(w, 200, result)
}
