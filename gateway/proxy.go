package gateway

import (
	"io"
	"log"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strconv"
	"strings"
)

func stripInternal(h http.Header) {
	for k := range h {
		low := strings.ToLower(k)
		if strings.HasPrefix(low, "x-openbear-") || strings.HasPrefix(low, "x-forwarded-") || low == "forwarded" || low == "x-real-ip" || low == "cf-connecting-ip" || low == "true-client-ip" || low == "cookie" || low == "proxy-authorization" {
			delete(h, k)
		}
	}
}

// Drop only the reserved context argument; preserve bytes, order, repetitions,
// and escaping of every business argument (including signed download URLs).
func withoutContext(raw string) string {
	parts := strings.Split(raw, "&")
	out := parts[:0]
	for _, part := range parts {
		key, _, _ := strings.Cut(part, "=")
		decoded, e := url.QueryUnescape(key)
		if e == nil && decoded == "_ob_ctx" {
			continue
		}
		out = append(out, part)
	}
	return strings.Join(out, "&")
}

// A non-empty upstreamPath replaces the request path on the node.
func (g *Gateway) proxy(w http.ResponseWriter, r *http.Request, i instance, s *session, upstreamPath string) {
	target, _ := url.Parse(i.Upstream)
	proxy := &httputil.ReverseProxy{
		Transport: g.transport, FlushInterval: -1,
		ErrorLog: log.New(io.Discard, "", 0),
		Rewrite: func(p *httputil.ProxyRequest) {
			p.SetURL(target)
			// Rewrite deliberately does not trust browser-supplied forwarding headers.
			stripInternal(p.Out.Header)
			if upstreamPath != "" {
				p.Out.URL.Path = upstreamPath
				p.Out.URL.RawPath = ""
			}
			if s != nil {
				p.Out.URL.RawQuery = withoutContext(p.In.URL.RawQuery)
				p.Out.Header.Del("Authorization")
				p.Out.Header.Set("X-OpenBear-Gateway-Key", i.BusinessKey)
				p.Out.Header.Set("X-OpenBear-Instance", i.ID)
				p.Out.Header.Set("X-OpenBear-Gateway-Session", s.UpstreamID)
				p.Out.Header.Set("X-OpenBear-Gateway-Origin", g.cfg.PublicURL)
				p.Out.Header.Set("X-OpenBear-Gateway-Expires", strconv.FormatInt(s.ExpiresAt, 10))
				p.Out.Header.Set(assetPrefixHeader, assetPrefix(i.ID))
			} else {
				p.Out.URL.RawQuery = p.In.URL.RawQuery
			}
		},
		ModifyResponse: func(res *http.Response) error {
			// The gateway alone owns browser authentication cookies and site state.
			res.Header.Del("Set-Cookie")
			res.Header.Del("Clear-Site-Data")
			for k := range res.Header {
				if strings.HasPrefix(strings.ToLower(k), "x-openbear-") {
					res.Header.Del(k)
				}
			}
			if s != nil {
				res.Header.Set("Cache-Control", "no-store")
				if immutableAsset(r) && (res.StatusCode == http.StatusOK || res.StatusCode == http.StatusPartialContent || res.StatusCode == http.StatusNotModified) {
					res.Header.Set("Cache-Control", "private, max-age=31536000, immutable")
				}
				res.Header.Del("CDN-Cache-Control")
				res.Header.Del("Surrogate-Control")
			}
			if location := res.Header.Get("Location"); location != "" && s != nil {
				u, e := url.Parse(location)
				if e == nil {
					resolved := target.ResolveReference(u)
					internal := (resolved.Scheme == "http" || resolved.Scheme == "https") && origin(resolved) == origin(target)
					if internal {
						if u.Host != "" {
							u.Scheme = ""
							u.Host = ""
						}
						if r.Header.Get("X-OpenBear-Context") == "" && r.URL.Query().Get("_ob_ctx") != "" {
							u.RawQuery = withoutContext(u.RawQuery)
							if u.RawQuery != "" {
								u.RawQuery += "&"
							}
							u.RawQuery += "_ob_ctx=" + url.QueryEscape(s.Context)
						}
						res.Header.Set("Location", u.String())
					}
				}
			}
			return nil
		},
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, _ error) { fail(w, 502, "upstream_unavailable") },
	}
	proxy.ServeHTTP(w, r)
}
func (g *Gateway) webhook(w http.ResponseWriter, r *http.Request) {
	parts := strings.Split(strings.TrimPrefix(r.URL.Path, "/i/"), "/")
	if len(parts) < 3 || !validID(parts[0]) || parts[1] != "webhook" {
		fail(w, 404, "not_found")
		return
	}
	rest := parts[2:]
	allowed := false
	if len(rest) == 1 && validID(rest[0]) && r.Method == "POST" {
		allowed = true
	}
	if len(rest) == 3 && validID(rest[0]) && rest[1] == "events" && validID(rest[2]) && (r.Method == "GET" || r.Method == "HEAD") {
		allowed = true
	}
	if len(rest) == 3 && rest[0] == "attempts" && validID(rest[1]) && rest[2] == "telemetry" && r.Method == "POST" {
		allowed = true
	}
	if !allowed {
		fail(w, 404, "not_found")
		return
	}
	g.mu.Lock()
	i, exists := g.state.Instances[parts[0]]
	bad := g.storageErr != nil
	g.mu.Unlock()
	if bad {
		fail(w, 503, "state_unavailable")
		return
	}
	if !exists || !i.Enabled {
		fail(w, 404, "not_found")
		return
	}
	g.proxy(w, r, i, nil, "/webhook/"+strings.Join(rest, "/"))
}
