package node

import (
	"context"
	"encoding/json"
	"errors"
	"math/rand/v2"
	"net"
	"net/netip"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode/utf8"
)

// Dynamic DNS: the controller hands this node the address probes its records
// need (ddns.watch) and writes DNS itself; the node only reports what it sees.
// Probes live as long as the session: the controller sends them again after
// every hello.
const (
	ddnsMaxProbes   = 64
	ddnsMaxURLs     = 8
	ddnsMaxSTUN     = 4
	ddnsMaxCmd      = 1000
	ddnsFailLimit   = 3
	ddnsMaxInterval = 3600
	ddnsProbeWait   = 20 * time.Second
)

var (
	errDDNSParams = errors.New("invalid_params")
	// Used when a probe names no URLs or STUN servers, as ddns.probe may.
	ddnsDefaultURLs = map[bool][]string{
		false: {"https://www.cloudflare.com/cdn-cgi/trace", "https://api.ip.sb/ip", "https://4.icanhazip.com", "https://api.ipify.org"},
		true:  {"https://www.cloudflare.com/cdn-cgi/trace", "https://api.ip.sb/ip", "https://6.icanhazip.com", "https://api6.ipify.org"},
	}
	ddnsDefaultSTUN = []string{"stun.cloudflare.com:3478", "stun.l.google.com:19302", "stun.miwifi.com:3478"}
)

// ddnsError is a failed reading; Error is the code the controller sees.
type ddnsError struct{ code, detail string }

func (e *ddnsError) Error() string { return e.code }

func ddnsFail(code, detail string) *ddnsError { return &ddnsError{code, detail} }

func ddnsCode(err error) (string, string) {
	var e *ddnsError
	if errors.As(err, &e) {
		return e.code, e.detail
	}
	return "parse_error", err.Error()
}

// ddnsPermanent errors do not wait for ddnsFailLimit: retrying cannot fix them.
func ddnsPermanent(code string) bool { return code == "capability_disabled" || code == "unsupported" }

type ddnsSelectIn struct {
	Mode           string `json:"mode"`
	Value          string `json:"value"`
	AllowTemporary bool   `json:"allowTemporary"`
}

type ddnsProbeIn struct {
	Key          string        `json:"key"`
	Family       string        `json:"family"`
	Method       string        `json:"method"`
	IntervalS    *int          `json:"intervalS"`
	Confirm      *int          `json:"confirm"`
	URLs         []string      `json:"urls"`
	BindIface    string        `json:"bindIface"`
	Iface        string        `json:"iface"`
	STUN         []string      `json:"stun"`
	Cmd          string        `json:"cmd"`
	V6Select     *ddnsSelectIn `json:"v6Select"`
	AllowPrivate bool          `json:"allowPrivate"`
}

// ddnsSelect picks one of a NIC's usable IPv6 addresses. Index counts from 1,
// as ddns-go's "@n" does.
type ddnsSelect struct {
	mode           string
	index          int
	prefix         netip.Prefix
	text           string
	re             *regexp.Regexp
	allowTemporary bool
}

type ddnsSpec struct {
	key, family, method string
	v6                  bool
	interval            time.Duration
	confirm             int
	urls, stun          []string
	bindIface, iface    string
	cmd                 string
	sel                 ddnsSelect
	allowPrivate        bool
}

// spec checks one probe; watch adds the key, interval and confirm count that
// a one-off diagnosis has no use for.
func (in ddnsProbeIn) spec(watch bool) (ddnsSpec, error) {
	s := ddnsSpec{key: in.Key, family: in.Family, method: in.Method, bindIface: in.BindIface, iface: in.Iface,
		cmd: in.Cmd, allowPrivate: in.AllowPrivate}
	switch in.Family {
	case "v4":
	case "v6":
		s.v6 = true
	default:
		return s, errDDNSParams
	}
	if len(in.Iface) > 256 || len(in.BindIface) > 256 || strings.ContainsRune(in.Iface+in.BindIface, 0) {
		return s, errDDNSParams
	}
	sel, err := ddnsParseSelect(in.V6Select)
	if err != nil {
		return s, err
	}
	s.sel = sel
	if len(in.URLs) > ddnsMaxURLs || len(in.STUN) > ddnsMaxSTUN {
		return s, errDDNSParams
	}
	switch in.Method {
	case "url":
		s.urls = in.URLs
		if len(s.urls) == 0 {
			s.urls = ddnsDefaultURLs[s.v6]
		}
		for _, u := range s.urls {
			if !ddnsValidURL(u) {
				return s, errDDNSParams
			}
		}
	case "stun":
		list := in.STUN
		if len(list) == 0 {
			list = ddnsDefaultSTUN
		}
		for _, server := range list {
			hostport, ok := ddnsSTUNServer(server)
			if !ok {
				return s, errDDNSParams
			}
			s.stun = append(s.stun, hostport)
		}
	case "cmd":
		if strings.TrimSpace(in.Cmd) == "" || utf8.RuneCountInString(in.Cmd) > ddnsMaxCmd || !utf8.ValidString(in.Cmd) {
			return s, errDDNSParams
		}
	case "nic":
	default:
		return s, errDDNSParams
	}
	if !watch {
		return s, nil
	}
	if in.Key == "" || len(in.Key) > 128 {
		return s, errDDNSParams
	}
	low, def := 30, 60
	if in.Method == "nic" {
		low, def = 10, 15
	}
	interval := def
	if in.IntervalS != nil {
		interval = *in.IntervalS
	}
	confirm := 2
	if in.Confirm != nil {
		confirm = *in.Confirm
	}
	if interval < low || interval > ddnsMaxInterval || confirm < 1 || confirm > 5 {
		return s, errDDNSParams
	}
	s.interval, s.confirm = time.Duration(interval)*time.Second, confirm
	return s, nil
}

func ddnsParseSelect(in *ddnsSelectIn) (ddnsSelect, error) {
	if in == nil {
		return ddnsSelect{mode: "first"}, nil
	}
	s := ddnsSelect{mode: in.Mode, allowTemporary: in.AllowTemporary}
	if s.mode == "" {
		s.mode = "first"
	}
	if len(in.Value) > 256 {
		return s, errDDNSParams
	}
	value := strings.TrimSpace(in.Value)
	switch s.mode {
	case "first":
	case "index":
		n, err := strconv.Atoi(value)
		if err != nil || n < 1 || n > 1000 {
			return s, errDDNSParams
		}
		s.index = n
	case "prefix":
		if value == "" {
			return s, errDDNSParams
		}
		if p, err := netip.ParsePrefix(value); err == nil && p.Addr().Is6() {
			s.prefix = p.Masked()
		} else {
			s.text = strings.ToLower(value)
		}
	case "regex":
		re, err := regexp.Compile(in.Value)
		if in.Value == "" || err != nil {
			return s, errDDNSParams
		}
		s.re = re
	default:
		return s, errDDNSParams
	}
	return s, nil
}

func ddnsValidURL(raw string) bool {
	if len(raw) > 2048 || strings.ContainsAny(raw, " \t\r\n") {
		return false
	}
	u, err := url.Parse(raw)
	return err == nil && (u.Scheme == "http" || u.Scheme == "https") && u.Hostname() != ""
}

// ddnsSTUNServer normalises "host", "host:port" or "stun:host:port" to host:port.
func ddnsSTUNServer(s string) (string, bool) {
	s = strings.TrimPrefix(strings.TrimSpace(s), "stun:")
	host, port := s, "3478"
	if h, p, err := net.SplitHostPort(s); err == nil {
		host, port = h, p
	} else if strings.HasPrefix(s, "[") && strings.HasSuffix(s, "]") {
		host = s[1 : len(s)-1]
	}
	n, err := strconv.Atoi(port)
	if err != nil || n < 1 || n > 65535 || !ddnsValidHost(host) {
		return "", false
	}
	return net.JoinHostPort(host, port), true
}

func ddnsValidHost(s string) bool {
	if s == "" || len(s) > 253 {
		return false
	}
	if _, err := netip.ParseAddr(s); err == nil {
		return !strings.Contains(s, "%")
	}
	for _, c := range s {
		if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '-' || c == '.' || c == '_') {
			return false
		}
	}
	return true
}

func parseDDNSWatch(raw json.RawMessage) ([]ddnsSpec, error) {
	var in struct {
		Probes []ddnsProbeIn `json:"probes"`
	}
	if json.Unmarshal(raw, &in) != nil || len(in.Probes) > ddnsMaxProbes {
		return nil, errDDNSParams
	}
	seen := map[string]bool{}
	specs := make([]ddnsSpec, 0, len(in.Probes))
	for _, p := range in.Probes {
		s, err := p.spec(true)
		if err != nil || seen[s.key] {
			return nil, errDDNSParams
		}
		seen[s.key] = true
		specs = append(specs, s)
	}
	return specs, nil
}

func (a *Agent) ddnsCall(ctx context.Context, m envelope) (any, error) {
	switch m.Method {
	case "ddns.watch":
		specs, err := parseDDNSWatch(m.Params)
		if err != nil {
			return nil, err
		}
		ch := a.currentChannel()
		if ch == nil {
			return nil, errors.New("not_connected")
		}
		// ctx is the session's: its probes end with it.
		a.ddns.watch(ctx, specs, a.ddnsRead, func(obs []ddnsObs) error {
			return ch.send(map[string]any{"t": "ddns", "obs": obs})
		})
		return map[string]any{"accepted": len(specs)}, nil
	case "ddns.probe":
		return a.ddnsProbe(ctx, m.Params)
	}
	return nil, errors.New("unsupported_method")
}

// ddnsObs is one reported observation; Fails counts the failures in a row.
type ddnsObs struct {
	Key    string `json:"key"`
	Family string `json:"family"`
	Method string `json:"method"`
	Value  string `json:"value"`
	Error  string `json:"error"`
	Detail string `json:"detail"`
	At     int64  `json:"at"`
	Fails  int    `json:"fails"`
}

type ddnsReader func(ctx context.Context, s ddnsSpec) (value, detail string, err error)

// ddnsWatcher runs the probes of the latest ddns.watch. The zero value is ready.
type ddnsWatcher struct {
	mu   sync.Mutex
	stop context.CancelFunc
	// after replaces time.After in tests.
	after func(time.Duration) <-chan time.Time
}

// watch replaces the running probes with specs, which run until ctx ends or
// the next watch. Each reports through send.
func (w *ddnsWatcher) watch(ctx context.Context, specs []ddnsSpec, read ddnsReader, send func([]ddnsObs) error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.stop != nil {
		w.stop()
		w.stop = nil
	}
	if len(specs) == 0 {
		return
	}
	ctx, w.stop = context.WithCancel(ctx)
	out := make(chan ddnsObs, len(specs))
	go ddnsDeliver(ctx, out, send)
	for _, s := range specs {
		go w.run(ctx, s, read, out)
	}
}

// ddnsDeliver sends observations as they come, together when several are waiting.
func ddnsDeliver(ctx context.Context, out <-chan ddnsObs, send func([]ddnsObs) error) {
	for {
		select {
		case <-ctx.Done():
			return
		case o := <-out:
			batch := []ddnsObs{o}
			for more := true; more; {
				select {
				case o := <-out:
					batch = append(batch, o)
				default:
					more = false
				}
			}
			// A failed send means the session is ending, which stops this too.
			send(batch)
		}
	}
}

func (w *ddnsWatcher) run(ctx context.Context, s ddnsSpec, read ddnsReader, out chan<- ddnsObs) {
	after := w.after
	if after == nil {
		after = time.After
	}
	t := ddnsTracker{confirm: s.confirm}
	for ctx.Err() == nil {
		value, detail, err := read(ctx, s)
		if ctx.Err() != nil {
			return
		}
		if o, ok := t.observe(value, detail, err); ok {
			o.Key, o.Family, o.Method, o.At = s.key, s.family, s.method, time.Now().UnixMilli()
			select {
			case out <- o:
			case <-ctx.Done():
				return
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-after(ddnsJitter(s.interval)):
		}
	}
}

// ddnsJitter spreads d by ±10% so probes started together drift apart.
func ddnsJitter(d time.Duration) time.Duration {
	return time.Duration(float64(d) * (0.9 + 0.2*rand.Float64()))
}

// ddnsTracker decides which readings of one probe are worth reporting: the
// first value, a new value once it was read confirm times in a row, the
// ddnsFailLimit-th failure in a row and the first value after it.
type ddnsTracker struct {
	confirm  int
	reported bool
	value    string
	next     string
	seen     int
	fails    int
	down     bool
}

func (t *ddnsTracker) observe(value, detail string, err error) (ddnsObs, bool) {
	if err != nil {
		t.fails++
		t.next, t.seen = "", 0
		code, detail := ddnsCode(err)
		if t.down || t.fails < ddnsFailLimit && !ddnsPermanent(code) {
			return ddnsObs{}, false
		}
		t.down = true
		return ddnsObs{Error: code, Detail: detail, Fails: t.fails}, true
	}
	t.fails = 0
	switch {
	case !t.reported || t.down:
	case value == t.value:
		t.next, t.seen = "", 0
		return ddnsObs{}, false
	default:
		if value != t.next {
			t.next, t.seen = value, 0
		}
		t.seen++
		if t.seen < max(t.confirm, 1) {
			return ddnsObs{}, false
		}
	}
	t.reported, t.down, t.value, t.next, t.seen = true, false, value, "", 0
	return ddnsObs{Value: value, Detail: detail}, true
}

type ddnsResult struct {
	Method string `json:"method"`
	Value  string `json:"value"`
	Error  string `json:"error"`
	Detail string `json:"detail"`
}

// ddnsProbe reads every requested method once, side by side, for diagnosis.
func (a *Agent) ddnsProbe(ctx context.Context, raw json.RawMessage) (any, error) {
	var in struct {
		ddnsProbeIn
		Methods []string `json:"methods"`
	}
	if json.Unmarshal(raw, &in) != nil || len(in.Methods) > 4 {
		return nil, errDDNSParams
	}
	methods := in.Methods
	if len(methods) == 0 {
		methods = []string{"url", "nic", "stun"}
	}
	specs := make([]ddnsSpec, len(methods))
	for i, method := range methods {
		p := in.ddnsProbeIn
		p.Method = method
		s, err := p.spec(false)
		if err != nil {
			return nil, err
		}
		for _, other := range methods[:i] {
			if other == method {
				return nil, errDDNSParams
			}
		}
		specs[i] = s
	}
	ctx, cancel := context.WithTimeout(ctx, ddnsProbeWait)
	defer cancel()
	results := make([]ddnsResult, len(specs))
	var wg sync.WaitGroup
	for i, s := range specs {
		wg.Add(1)
		go func() {
			defer wg.Done()
			value, detail, err := a.ddnsRead(ctx, s)
			r := ddnsResult{Method: s.method, Value: value, Detail: detail}
			if err != nil {
				r.Value = ""
				r.Error, r.Detail = ddnsCode(err)
			}
			results[i] = r
		}()
	}
	wg.Wait()
	return map[string]any{"results": results, "interfaces": ddnsInterfaces()}, nil
}

type ddnsV6Addr struct {
	Addr       string `json:"addr"`
	Temporary  bool   `json:"temporary"`
	Deprecated bool   `json:"deprecated"`
	Scope      string `json:"scope"`
}

type ddnsIfaceInfo struct {
	Name    string       `json:"name"`
	Default bool         `json:"default"`
	V4      []string     `json:"v4"`
	V6      []ddnsV6Addr `json:"v6"`
}

// ddnsInterfaces lists the up, non-loopback interfaces that have addresses;
// Default marks the one the default route of either family leaves through.
func ddnsInterfaces() []ddnsIfaceInfo {
	out := []ddnsIfaceInfo{}
	list, err := net.Interfaces()
	if err != nil {
		return out
	}
	flags, _ := ddnsV6Flags()
	def4, _ := ddnsRouteIface(false)
	def6, _ := ddnsRouteIface(true)
	for _, ifi := range list {
		if ifi.Flags&net.FlagUp == 0 || ifi.Flags&net.FlagLoopback != 0 {
			continue
		}
		info := ddnsIfaceInfo{Name: ifi.Name, Default: ifi.Name == def4 || ifi.Name == def6, V4: []string{}, V6: []ddnsV6Addr{}}
		for _, addr := range ddnsIfaceAddrs(&ifi) {
			if addr.Is4() {
				info.V4 = append(info.V4, addr.String())
				continue
			}
			f := flags[ddnsAddrKey{ifi.Index, addr}]
			info.V6 = append(info.V6, ddnsV6Addr{Addr: addr.String(), Temporary: f.temporary, Deprecated: f.deprecated, Scope: ddnsScope(addr)})
		}
		if len(info.V4)+len(info.V6) > 0 {
			out = append(out, info)
		}
	}
	return out
}

func ddnsScope(addr netip.Addr) string {
	switch {
	case addr.IsLinkLocalUnicast():
		return "link"
	case addr.IsPrivate():
		return "ula"
	case addr.IsGlobalUnicast():
		return "global"
	}
	return "other"
}
