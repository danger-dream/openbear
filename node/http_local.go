package node

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

// http.local makes one HTTP request to a service on this machine's loopback
// (a managed instance's management API, the access gateway's control
// listener) and returns the response over the encrypted controller channel,
// so those services never listen where the public network reaches them.
const (
	httpLocalMaxBody        = 2 << 20
	httpLocalDefaultTimeout = 20 * time.Second
	httpLocalMaxTimeout     = 60 * time.Second
)

var httpLocalMethods = map[string]bool{"GET": true, "POST": true, "PUT": true, "PATCH": true, "DELETE": true}

// Credentials and content negotiation only; Host, cookies and hop-by-hop
// headers are never taken from the controller.
var httpLocalHeaders = map[string]bool{"Authorization": true, "Content-Type": true, "Accept": true}

type httpLocalParams struct {
	Method    string            `json:"method"`
	Port      int               `json:"port"`
	Path      string            `json:"path"`
	Headers   map[string]string `json:"headers"`
	Body      string            `json:"body"`
	TimeoutMs int               `json:"timeoutMs"`
}

// httpLocal reaches services the controller could otherwise only reach by
// running commands here, so it needs the same consent.
func (a *Agent) httpLocal(ctx context.Context, raw json.RawMessage) (any, error) {
	if !a.config().Capabilities.Exec {
		return nil, errors.New("capability_disabled")
	}
	return httpLocal(ctx, raw)
}

func httpLocal(ctx context.Context, raw json.RawMessage) (map[string]any, error) {
	var p httpLocalParams
	invalid := errors.New("invalid_params")
	if json.Unmarshal(raw, &p) != nil || !httpLocalMethods[p.Method] || p.Port < 1 || p.Port > 65535 {
		return nil, invalid
	}
	if !strings.HasPrefix(p.Path, "/") || strings.HasPrefix(p.Path, "//") || len(p.Path) > 4096 || strings.ContainsAny(p.Path, "#\\") {
		return nil, invalid
	}
	for _, c := range p.Path {
		if c <= 32 || c == 127 {
			return nil, invalid
		}
	}
	address := net.JoinHostPort("127.0.0.1", strconv.Itoa(p.Port))
	u, err := url.Parse("http://" + address + p.Path)
	if err != nil || u.Host != address || u.User != nil {
		return nil, invalid
	}
	body, err := base64.StdEncoding.DecodeString(p.Body)
	if err != nil || len(body) > httpLocalMaxBody {
		return nil, invalid
	}
	timeout := httpLocalDefaultTimeout
	if p.TimeoutMs != 0 {
		timeout = time.Duration(p.TimeoutMs) * time.Millisecond
		if timeout <= 0 || timeout > httpLocalMaxTimeout {
			return nil, invalid
		}
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, p.Method, u.String(), bytes.NewReader(body))
	if err != nil {
		return nil, invalid
	}
	for name, value := range p.Headers {
		if !httpLocalHeaders[http.CanonicalHeaderKey(name)] || strings.ContainsAny(value, "\r\n\x00") || len(value) > 8192 {
			return nil, invalid
		}
		req.Header.Set(name, value)
	}
	dialer := &net.Dialer{Timeout: 5 * time.Second}
	client := &http.Client{
		Transport: &http.Transport{
			Proxy:             nil,
			DisableKeepAlives: true,
			// Only the one loopback address and port, whatever the URL says.
			DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
				if addr != address {
					return nil, errors.New("loopback only")
				}
				return dialer.DialContext(ctx, "tcp4", address)
			},
		},
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
	res, err := client.Do(req)
	if err != nil {
		if ctx.Err() != nil {
			return nil, errors.New("timeout")
		}
		return nil, errors.New("connection_failed")
	}
	defer res.Body.Close()
	data, err := io.ReadAll(io.LimitReader(res.Body, httpLocalMaxBody+1))
	if err != nil {
		if ctx.Err() != nil {
			return nil, errors.New("timeout")
		}
		return nil, errors.New("connection_failed")
	}
	if len(data) > httpLocalMaxBody {
		return nil, errors.New("response_too_large")
	}
	return map[string]any{"status": res.StatusCode, "contentType": res.Header.Get("Content-Type"),
		"body": base64.StdEncoding.EncodeToString(data)}, nil
}
