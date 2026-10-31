package gateway

import (
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/netip"
	"slices"
	"sort"
	"strconv"
	"strings"
	"time"
)

// Source "auto" trusts loopback peers as proxies, and Cloudflare's published
// edge ranges for CF-Connecting-IP only: Cloudflare overwrites that header, so
// a client cannot choose it. Any other proxy, including one on the same private
// network, is trusted only once configured: "header" adds those to auto's.
// Header is accepted only for compatibility with earlier policies: every known
// forwarding header is read in a fixed order from a trusted proxy.
type ClientIPConfig struct {
	Source         string   `json:"source"`
	Header         string   `json:"header,omitempty"`
	TrustedProxies []string `json:"trustedProxies"`
}
type clientIPPolicy struct {
	Config  ClientIPConfig
	Trusted []netip.Prefix
}

var forwardingHeaders = []string{"CF-Connecting-IP", "True-Client-IP", "X-Real-IP", "X-Forwarded-For", "Forwarded"}

// https://www.cloudflare.com/ips/ (stable for years; manual proxies cover changes).
var cloudflareRanges = mustPrefixes("173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "141.101.64.0/18",
	"108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15",
	"104.16.0.0/13", "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22", "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32",
	"2405:b500::/32", "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32")

func mustPrefixes(values ...string) []netip.Prefix {
	out := make([]netip.Prefix, 0, len(values))
	for _, v := range values {
		out = append(out, netip.MustParsePrefix(v))
	}
	return out
}
func defaultClientIP() ClientIPConfig {
	return ClientIPConfig{Source: "auto", TrustedProxies: []string{}}
}
func normalizeClientIP(c ClientIPConfig) (ClientIPConfig, error) {
	bad := errors.New("invalid_gateway_client_ip")
	if c.Source == "socket" && c.TrustedProxies == nil {
		c.TrustedProxies = []string{}
	}
	if (c.Source != "socket" && c.Source != "header" && c.Source != "auto") || c.TrustedProxies == nil || len(c.TrustedProxies) > 128 {
		return c, bad
	}
	if c.Header != "" {
		known := false
		for _, name := range forwardingHeaders {
			known = known || strings.EqualFold(c.Header, name)
		}
		if !known {
			return c, bad
		}
	}
	c.Header = ""
	unique := map[string]bool{}
	for _, entry := range c.TrustedProxies {
		if len(entry) > 128 {
			return c, bad
		}
		entry = strings.TrimSpace(entry)
		if strings.Contains(entry, "%") {
			return c, bad
		}
		host, bits, hasBits := strings.Cut(entry, "/")
		addr, err := netip.ParseAddr(host)
		if err != nil {
			return c, bad
		}
		n := addr.BitLen()
		if hasBits {
			if bits == "" {
				return c, bad
			}
			for _, r := range bits {
				if r < '0' || r > '9' {
					return c, bad
				}
			}
			n, err = strconv.Atoi(bits)
			if err != nil || n < 0 || n > addr.BitLen() {
				return c, bad
			}
		}
		prefix := netip.PrefixFrom(addr, n).Masked()
		// Match Python ipaddress's canonical IPv6 (including IPv4-mapped
		// addresses) instead of netip's mixed dotted mapped-address spelling.
		text := prefix.Addr().String()
		if prefix.Addr().Is4In6() {
			raw := prefix.Addr().As16()
			text = fmt.Sprintf("::ffff:%x:%x", uint16(raw[12])<<8|uint16(raw[13]), uint16(raw[14])<<8|uint16(raw[15]))
		}
		unique[text+"/"+strconv.Itoa(n)] = true
	}
	c.TrustedProxies = make([]string, 0, len(unique))
	for entry := range unique {
		c.TrustedProxies = append(c.TrustedProxies, entry)
	}
	sort.Strings(c.TrustedProxies)
	if c.Source == "header" && len(c.TrustedProxies) == 0 {
		return c, bad
	}
	if c.Source != "header" {
		c.TrustedProxies = []string{}
	}
	return c, nil
}
func compileClientIP(c ClientIPConfig) *clientIPPolicy {
	p := &clientIPPolicy{Config: c}
	for _, entry := range c.TrustedProxies {
		prefix, _ := netip.ParsePrefix(entry)
		p.Trusted = append(p.Trusted, prefix)
	}
	return p
}
func (c ClientIPConfig) equal(other ClientIPConfig) bool {
	return c.Source == other.Source && slices.Equal(c.TrustedProxies, other.TrustedProxies)
}
func (p *clientIPPolicy) trusts(addr netip.Addr) bool {
	if p.Config.Source == "socket" {
		return false
	}
	if addr.IsLoopback() {
		return true
	}
	for _, prefix := range p.Trusted {
		candidate := addr
		if prefix.Addr().Is4In6() && addr.Is4() {
			candidate = netip.AddrFrom16(addr.As16())
		}
		if prefix.Contains(candidate) {
			return true
		}
	}
	return false
}

// reads reports whether the real client IP may come from this peer's header.
func (p *clientIPPolicy) reads(peer netip.Addr, header string) bool {
	if p.trusts(peer) {
		return true
	}
	if p.Config.Source == "socket" || header != "CF-Connecting-IP" {
		return false
	}
	for _, prefix := range cloudflareRanges {
		if prefix.Contains(peer) {
			return true
		}
	}
	return false
}
func socketIP(r *http.Request) netip.Addr {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return netip.Addr{}
	}
	addr, err := netip.ParseAddr(host)
	if err != nil {
		return netip.Addr{}
	}
	return addr.WithZone("").Unmap()
}
func plainIP(value string) (netip.Addr, bool) {
	addr, err := netip.ParseAddr(strings.TrimSpace(value))
	return addr.Unmap(), err == nil && addr.Zone() == ""
}
func (g *Gateway) clientIP(r *http.Request) string {
	peer := socketIP(r)
	if !peer.IsValid() {
		return "unknown"
	}
	p := g.ipPolicy.Load()
	if p == nil || p.Config.Source == "socket" {
		return peer.String()
	}
	for _, name := range forwardingHeaders {
		values := r.Header.Values(name)
		if len(values) == 0 {
			continue
		}
		if !p.reads(peer, name) {
			// A private-network peer sending forwarding headers is reported for
			// the owner to confirm; it never chooses its own client identity.
			if peer.IsPrivate() {
				g.observe(peer, name, false)
			}
			return peer.String()
		}
		g.observe(peer, name, true)
		if ip, ok := p.fromHeader(name, values); ok {
			return ip.String()
		}
		return peer.String()
	}
	return peer.String()
}
func (p *clientIPPolicy) fromHeader(name string, values []string) (netip.Addr, bool) {
	if name != "X-Forwarded-For" && name != "Forwarded" {
		if len(values) != 1 {
			return netip.Addr{}, false
		}
		return plainIP(values[0])
	}
	text := strings.Join(values, ",")
	if len(text) > 8192 {
		return netip.Addr{}, false
	}
	var chain []netip.Addr
	if name == "X-Forwarded-For" {
		parts := strings.Split(text, ",")
		if len(parts) > 128 {
			return netip.Addr{}, false
		}
		for _, part := range parts {
			addr, ok := plainIP(part)
			if !ok {
				return netip.Addr{}, false
			}
			chain = append(chain, addr)
		}
	} else {
		var ok bool
		if chain, ok = parseForwarded(text); !ok {
			return netip.Addr{}, false
		}
	}
	for n := len(chain) - 1; n >= 0; n-- {
		if !p.trusts(chain[n]) || n == 0 {
			return chain[n], true
		}
	}
	return netip.Addr{}, false
}

// What the gateway actually sees, so the controller can explain detection
// instead of asking the user to choose a header. Trusted is false for a proxy
// whose header was ignored; earlier gateways reported only trusted proxies and
// omit the field.
type proxyObservation struct {
	Peer    string `json:"peer"`
	Header  string `json:"header"`
	AtMS    int64  `json:"atMs"`
	Trusted bool   `json:"trusted"`
}

func (g *Gateway) observe(peer netip.Addr, header string, trusted bool) {
	g.observed.Store(&proxyObservation{peer.String(), header, time.Now().UnixMilli(), trusted})
}
func (g *Gateway) observedProxy() *proxyObservation { return g.observed.Load() }

// RFC7239 list/parameter splitting preserves quoted delimiters and rejects
// malformed quoted strings instead of guessing a client identity.
func splitForwarded(s string, sep byte) ([]string, bool) {
	var parts []string
	start := 0
	quoted, escaped := false, false
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c < 32 && c != '\t' || c == 127 {
			return nil, false
		}
		if escaped {
			escaped = false
			continue
		}
		if quoted && c == '\\' {
			escaped = true
			continue
		}
		if c == '"' {
			quoted = !quoted
			continue
		}
		if !quoted && c == sep {
			part := strings.TrimSpace(s[start:i])
			if part == "" {
				return nil, false
			}
			parts = append(parts, part)
			start = i + 1
		}
	}
	if quoted || escaped {
		return nil, false
	}
	part := strings.TrimSpace(s[start:])
	if part == "" {
		return nil, false
	}
	return append(parts, part), true
}
func forwardedToken(s string) bool {
	if s == "" {
		return false
	}
	for _, c := range s {
		if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || strings.ContainsRune("!#$%&'*+-.^_`|~", c)) {
			return false
		}
	}
	return true
}
func forwardedValue(s string) (string, bool) {
	s = strings.TrimSpace(s)
	if !strings.HasPrefix(s, "\"") {
		return s, forwardedToken(s)
	}
	if len(s) < 2 || s[len(s)-1] != '"' {
		return "", false
	}
	var out strings.Builder
	for n := 1; n < len(s)-1; n++ {
		if s[n] == '\\' {
			n++
			if n >= len(s)-1 {
				return "", false
			}
			out.WriteByte(s[n])
			continue
		}
		if s[n] == '"' {
			return "", false
		}
		out.WriteByte(s[n])
	}
	return out.String(), true
}
func parseForwarded(s string) ([]netip.Addr, bool) {
	parts, ok := splitForwarded(s, ',')
	if !ok || len(parts) > 128 {
		return nil, false
	}
	chain := make([]netip.Addr, 0, len(parts))
	for _, part := range parts {
		params, ok := splitForwarded(part, ';')
		if !ok {
			return nil, false
		}
		value := ""
		found := false
		for _, param := range params {
			key, raw, exists := strings.Cut(param, "=")
			if !exists || !forwardedToken(strings.TrimSpace(key)) {
				return nil, false
			}
			parsed, valid := forwardedValue(raw)
			if !valid {
				return nil, false
			}
			if strings.EqualFold(strings.TrimSpace(key), "for") {
				if found {
					return nil, false
				}
				found = true
				value = parsed
			}
		}
		if !found {
			return nil, false
		}
		addr, ok := plainIP(value)
		if !ok && strings.HasPrefix(value, "[") && strings.HasSuffix(value, "]") {
			raw, err := netip.ParseAddr(value[1 : len(value)-1])
			ok = err == nil && raw.Is6() && raw.Zone() == ""
			addr = raw.Unmap()
		}
		if !ok {
			pair, err := netip.ParseAddrPort(value)
			addr = pair.Addr().Unmap()
			ok = err == nil && pair.Addr().Zone() == ""
		}
		if !ok {
			return nil, false
		}
		chain = append(chain, addr)
	}
	return chain, true
}
func (g *Gateway) putClientIP(w http.ResponseWriter, r *http.Request) {
	var policy ClientIPConfig
	if !decodeBody(w, r, &policy) {
		return
	}
	normalized, err := normalizeClientIP(policy)
	if err != nil {
		fail(w, 400, "invalid_gateway_client_ip")
		return
	}
	compiled := compileClientIP(normalized)
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.storageErr != nil {
		fail(w, 503, "state_unavailable")
		return
	}
	if !g.ipPolicy.Load().Config.equal(normalized) {
		if err = g.transaction(func(s *state) { s.ClientIP = &normalized }); err != nil {
			fail(w, 503, "state_unavailable")
			return
		}
		g.ipPolicy.Store(compiled)
		g.limits.Entries = nil // Old source identities must not share new-source debt.
		// A just-confirmed proxy must not keep showing as untrusted.
		if o := g.observed.Load(); o != nil {
			if peer, e := netip.ParseAddr(o.Peer); e == nil {
				next := *o
				next.Trusted = compiled.reads(peer, o.Header)
				g.observed.CompareAndSwap(o, &next)
			}
		}
	}
	jsonResponse(w, 200, map[string]any{"ok": true, "clientIp": g.ipPolicy.Load().Config, "clientIpObserved": g.observedProxy()})
}
