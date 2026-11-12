package node

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/cookiejar"
	"net/netip"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"openbear/node/mesh/bypass"
)

// net.http: the HTTPS requests the controller lists, made through one address family, each
// answered with its status, final address and what the listed patterns find in the body. The
// controller decides what the answers mean, so its checks change without a node update. Only
// public addresses are dialled; nothing of the body is sent back but those findings.

const (
	httpProbeMaxRequests = 40
	httpProbeMaxBody     = 4 << 20
	httpProbeWorkers     = 6
)

var (
	errNotPublic  = errors.New("not a public address")
	sharedAddress = netip.MustParsePrefix("100.64.0.0/10")
	// Tests reach a local TLS server through these.
	httpProbeDial = publicDestination
	httpProbeTLS  *tls.Config
)

type httpProbeRequest struct {
	ID        string            `json:"id"`
	URL       string            `json:"url"`
	Method    string            `json:"method"`
	Headers   map[string]string `json:"headers"`
	Body      string            `json:"body"`
	TimeoutMs int               `json:"timeoutMs"`
	// Find: the first group of the first match of each pattern (of the last with Last);
	// Contains: whether the body holds each text, ignoring case; JSON: the value at each
	// dotted path of a JSON body, numbers indexing arrays.
	Find     []httpProbeFind `json:"find"`
	Contains []string        `json:"contains"`
	JSON     []string        `json:"json"`
}

type httpProbeFind struct {
	Re   string `json:"re"`
	Last bool   `json:"last"`
}

type httpProbeResult struct {
	ID       string   `json:"id"`
	Status   int      `json:"status"`
	URL      string   `json:"url,omitempty"`
	Size     int      `json:"size"`
	Error    string   `json:"error,omitempty"`
	Find     []string `json:"find"`
	Contains []bool   `json:"contains"`
	JSON     []string `json:"json"`
}

type httpProbe struct {
	request  httpProbeRequest
	patterns []*regexp.Regexp
}

func (a *Agent) netHTTP(ctx context.Context, raw json.RawMessage) (any, error) {
	var p struct {
		Family   int                `json:"family"`
		Requests []httpProbeRequest `json:"requests"`
	}
	if json.Unmarshal(raw, &p) != nil || (p.Family != 4 && p.Family != 6) || len(p.Requests) == 0 || len(p.Requests) > httpProbeMaxRequests {
		return nil, errors.New("invalid_params")
	}
	probes := make([]httpProbe, len(p.Requests))
	for i, r := range p.Requests {
		probe, err := checkHTTPProbe(r)
		if err != nil {
			return nil, err
		}
		probes[i] = probe
	}
	network := map[int]string{4: "tcp4", 6: "tcp6"}[p.Family]
	results := make([]httpProbeResult, len(probes))
	var wg sync.WaitGroup
	gate := make(chan struct{}, httpProbeWorkers)
	for i := range probes {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			gate <- struct{}{}
			defer func() { <-gate }()
			results[i] = runHTTPProbe(ctx, network, probes[i])
		}(i)
	}
	wg.Wait()
	return map[string]any{"family": p.Family, "results": results}, nil
}

func checkHTTPProbe(r httpProbeRequest) (httpProbe, error) {
	if r.Method == "" {
		r.Method = http.MethodGet
	}
	if !strings.HasPrefix(r.URL, "https://") || len(r.URL) > 2048 || len(r.Body) > 16<<10 ||
		(r.Method != http.MethodGet && r.Method != http.MethodPost && r.Method != http.MethodHead) ||
		len(r.Find)+len(r.Contains)+len(r.JSON) > 20 {
		return httpProbe{}, errors.New("invalid_params")
	}
	probe := httpProbe{request: r}
	for _, f := range r.Find {
		re, err := regexp.Compile(f.Re)
		if err != nil || len(f.Re) > 400 {
			return httpProbe{}, errors.New("invalid_params")
		}
		probe.patterns = append(probe.patterns, re)
	}
	return probe, nil
}

// publicDestination refuses loopback, private, link-local and shared (CGNAT) addresses, on the
// first connection and after every redirect alike.
func publicDestination(_, address string, _ syscall.RawConn) error {
	host, _, err := net.SplitHostPort(address)
	if err != nil {
		return errNotPublic
	}
	ip, err := netip.ParseAddr(host)
	if err != nil {
		return errNotPublic
	}
	ip = ip.Unmap()
	if !ip.IsGlobalUnicast() || ip.IsPrivate() || sharedAddress.Contains(ip) {
		return errNotPublic
	}
	return nil
}

func runHTTPProbe(ctx context.Context, network string, probe httpProbe) httpProbeResult {
	r := probe.request
	result := httpProbeResult{ID: r.ID, Find: make([]string, len(r.Find)), Contains: make([]bool, len(r.Contains)), JSON: make([]string, len(r.JSON))}
	timeout := time.Duration(r.TimeoutMs) * time.Millisecond
	if timeout <= 0 || timeout > 20*time.Second {
		timeout = 10 * time.Second
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	dialer := &net.Dialer{Timeout: 8 * time.Second, Resolver: bypass.Resolver(), Control: func(network, address string, c syscall.RawConn) error {
		if err := httpProbeDial(network, address, c); err != nil {
			return err
		}
		return bypass.Control(network, address, c)
	}}
	transport := &http.Transport{
		DialContext: func(ctx context.Context, _, addr string) (net.Conn, error) {
			return bypass.DialContext(ctx, dialer, network, addr)
		},
		ForceAttemptHTTP2:   true,
		TLSHandshakeTimeout: 8 * time.Second,
		TLSClientConfig:     httpProbeTLS.Clone(),
	}
	defer transport.CloseIdleConnections()
	jar, _ := cookiejar.New(nil)
	client := &http.Client{Transport: transport, Jar: jar}
	req, err := http.NewRequestWithContext(ctx, r.Method, r.URL, strings.NewReader(r.Body))
	if err != nil {
		result.Error = "invalid request"
		return result
	}
	for k, v := range r.Headers {
		// The transport decompresses only what it asked for itself.
		if !strings.EqualFold(k, "Accept-Encoding") && !strings.EqualFold(k, "Host") && !strings.EqualFold(k, "Content-Length") {
			req.Header.Set(k, v)
		}
	}
	resp, err := client.Do(req)
	if err != nil {
		result.Error = probeError(err)
		return result
	}
	defer resp.Body.Close()
	result.Status, result.URL = resp.StatusCode, resp.Request.URL.String()
	body, err := io.ReadAll(io.LimitReader(resp.Body, httpProbeMaxBody))
	if err != nil && len(body) == 0 {
		result.Error = probeError(err)
		return result
	}
	result.Size = len(body)
	for i, re := range probe.patterns {
		matches := re.FindAllSubmatch(body, -1)
		if len(matches) == 0 {
			continue
		}
		m := matches[0]
		if r.Find[i].Last {
			m = matches[len(matches)-1]
		}
		result.Find[i] = string(m[len(m)-1])
	}
	if len(r.Contains) > 0 {
		lower := strings.ToLower(string(body))
		for i, text := range r.Contains {
			result.Contains[i] = strings.Contains(lower, strings.ToLower(text))
		}
	}
	if len(r.JSON) > 0 {
		var doc any
		if json.Unmarshal(body, &doc) == nil {
			for i, path := range r.JSON {
				result.JSON[i] = jsonAt(doc, path)
			}
		}
	}
	return result
}

func jsonAt(doc any, path string) string {
	for _, key := range strings.Split(path, ".") {
		switch v := doc.(type) {
		case map[string]any:
			doc = v[key]
		case []any:
			i, err := strconv.Atoi(key)
			if err != nil || i < 0 || i >= len(v) {
				return ""
			}
			doc = v[i]
		default:
			return ""
		}
	}
	switch v := doc.(type) {
	case string:
		return v
	case float64, bool:
		return fmt.Sprint(v)
	}
	return ""
}

func probeError(err error) string {
	var dns *net.DNSError
	switch {
	case errors.Is(err, errNotPublic):
		return "not_public"
	case errors.Is(err, context.DeadlineExceeded):
		return "timeout"
	case errors.As(err, &dns):
		return "dns"
	}
	var op *net.OpError
	if errors.As(err, &op) && op.Op == "dial" {
		return "connect"
	}
	text := err.Error()
	if len(text) > 160 {
		text = text[:160]
	}
	return text
}
