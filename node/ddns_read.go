package node

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"sort"
	"strings"
	"syscall"
	"time"
)

const (
	ddnsBodyLimit   = 64 << 10
	ddnsHTTPTimeout = 8 * time.Second
	ddnsCmdTimeout  = 10
	ddnsDetailLimit = 300
)

var ddnsCGNAT = netip.MustParsePrefix("100.64.0.0/10")

func ddnsFamilyName(v6 bool) string {
	if v6 {
		return "IPv6"
	}
	return "IPv4"
}

// ddnsRead takes one reading of s: this machine's address and, for the
// controller, what it came from.
func (a *Agent) ddnsRead(ctx context.Context, s ddnsSpec) (string, string, error) {
	var addr netip.Addr
	var detail string
	var err error
	switch s.method {
	case "url":
		addr, detail, err = ddnsURL(ctx, s.urls, s.v6, s.bindIface)
	case "nic":
		addr, detail, err = ddnsNIC(s)
	case "stun":
		addr, detail, err = ddnsSTUN(ctx, s.stun, s.v6, s.bindIface)
	case "cmd":
		if !a.config().Capabilities.Exec {
			return "", "", ddnsFail("capability_disabled", "command execution is off on this node")
		}
		addr, err = ddnsCmd(ctx, s.cmd, s.v6)
	default:
		err = ddnsFail("unsupported", s.method)
	}
	if err != nil {
		return "", "", err
	}
	return addr.String(), detail, nil
}

// ddnsUsable tells whether addr can be this machine's address in a reply or
// a command's output: IPv4 not mapped into IPv6, or global unicast IPv6.
// Private ranges pass; the controller filters them per record.
func ddnsUsable(addr netip.Addr, v6 bool) bool {
	if addr.Zone() != "" || addr.Is4In6() {
		return false
	}
	if v6 {
		return addr.Is6() && addr.IsGlobalUnicast()
	}
	return addr.Is4() && !addr.IsUnspecified() && !addr.IsLoopback() && !addr.IsMulticast() && addr != netip.AddrFrom4([4]byte{255, 255, 255, 255})
}

// ddnsJSONKeys name the caller's address in JSON answers, most specific first: some answers
// also carry the server's own address ahead of it (upyun's addr before remote_addr).
var ddnsJSONKeys = []string{"remote_addr", "client_ip", "clientip", "ip", "query", "origin", "data"}

// ddnsParse finds the first usable address in text: a Cloudflare trace's "ip=" line,
// else in JSON the value of a ddnsJSONKeys key, else the first address-looking word.
func ddnsParse(text string, v6 bool) (netip.Addr, bool) {
	for _, line := range strings.Split(text, "\n") {
		if v, ok := strings.CutPrefix(strings.TrimSpace(line), "ip="); ok {
			if addr, err := netip.ParseAddr(strings.TrimSpace(v)); err == nil && ddnsUsable(addr, v6) {
				return addr, true
			}
		}
	}
	var doc any
	if json.Unmarshal([]byte(text), &doc) == nil {
		if addr, ok := ddnsFromJSON(doc, v6, 0); ok {
			return addr, true
		}
	}
	return ddnsScan(text, v6)
}

// ddnsFromJSON looks for ddnsJSONKeys in an object, then in the objects inside it.
func ddnsFromJSON(doc any, v6 bool, depth int) (netip.Addr, bool) {
	obj, ok := doc.(map[string]any)
	if !ok || depth > 3 {
		return netip.Addr{}, false
	}
	keys := make([]string, 0, len(obj))
	lower := make(map[string]any, len(obj))
	for k, v := range obj {
		keys = append(keys, k)
		lower[strings.ToLower(k)] = v
	}
	for _, k := range ddnsJSONKeys {
		if s, ok := lower[k].(string); ok {
			if addr, ok := ddnsScan(s, v6); ok {
				return addr, true
			}
		}
	}
	sort.Strings(keys)
	for _, k := range keys {
		if addr, ok := ddnsFromJSON(obj[k], v6, depth+1); ok {
			return addr, true
		}
	}
	return netip.Addr{}, false
}

// ddnsScan takes the first usable address in free text.
func ddnsScan(text string, v6 bool) (netip.Addr, bool) {
	words := strings.FieldsFunc(text, func(r rune) bool {
		return !(r >= '0' && r <= '9' || r >= 'a' && r <= 'f' || r >= 'A' && r <= 'F' || r == ':' || r == '.')
	})
	for _, w := range words {
		for _, candidate := range []string{w, strings.Trim(w, ":.")} {
			if addr, err := netip.ParseAddr(candidate); err == nil {
				if ddnsUsable(addr, v6) {
					return addr, true
				}
				break
			}
		}
	}
	return netip.Addr{}, false
}

func ddnsClip(s string) string {
	s = strings.TrimSpace(s)
	if len(s) > ddnsDetailLimit {
		s = s[:ddnsDetailLimit] + "…"
	}
	return s
}

// ddnsNetError classifies a failure to reach a service.
func ddnsNetError(err error) *ddnsError {
	var ne net.Error
	var addrErr *net.AddrError
	switch {
	case errors.Is(err, context.DeadlineExceeded), errors.As(err, &ne) && ne.Timeout():
		return ddnsFail("timeout", "")
	case errors.Is(err, syscall.ENETUNREACH), errors.Is(err, syscall.EADDRNOTAVAIL),
		errors.As(err, &addrErr) && strings.Contains(addrErr.Err, "no suitable address"):
		return ddnsFail("no_address", ddnsClip(err.Error()))
	}
	return ddnsFail("http_error", ddnsClip(err.Error()))
}

// ddnsDialer dials one family without a proxy, from bindIface when set.
func ddnsDialer(bindIface string, v6, udp bool) (*net.Dialer, error) {
	d := &net.Dialer{Timeout: ddnsHTTPTimeout}
	if bindIface == "" {
		return d, nil
	}
	ifi, err := net.InterfaceByName(bindIface)
	if err != nil {
		return nil, ddnsFail("iface_not_found", bindIface)
	}
	if ddnsBindDevice(d, ifi.Name) {
		return d, nil
	}
	local, ok := ddnsLocalAddr(ifi, v6)
	if !ok {
		return nil, ddnsFail("no_address", "no "+ddnsFamilyName(v6)+" address on "+ifi.Name)
	}
	if udp {
		d.LocalAddr = &net.UDPAddr{IP: local.AsSlice()}
	} else {
		d.LocalAddr = &net.TCPAddr{IP: local.AsSlice()}
	}
	return d, nil
}

// ddnsLocalAddr is the address to send from when binding to ifi by address:
// a long-lived global one before a private one.
func ddnsLocalAddr(ifi *net.Interface, v6 bool) (netip.Addr, bool) {
	if !v6 {
		list := ddnsNICv4(ddnsIfaceAddrs(ifi))
		if len(list) == 0 {
			return netip.Addr{}, false
		}
		return list[0], true
	}
	flags, _ := ddnsV6Flags()
	var private netip.Addr
	for _, c := range ddnsV6Candidates(ifi, flags) {
		if c.temporary || c.deprecated || !c.addr.IsGlobalUnicast() {
			continue
		}
		if !c.addr.IsPrivate() {
			return c.addr, true
		}
		if !private.IsValid() {
			private = c.addr
		}
	}
	return private, private.IsValid()
}

func ddnsHTTPClient(d *net.Dialer, v6 bool) *http.Client {
	network := "tcp4"
	if v6 {
		network = "tcp6"
	}
	transport := &http.Transport{
		Proxy: nil,
		DialContext: func(ctx context.Context, _, addr string) (net.Conn, error) {
			return d.DialContext(ctx, network, addr)
		},
		DisableKeepAlives:   true,
		TLSHandshakeTimeout: ddnsHTTPTimeout,
	}
	return &http.Client{Timeout: ddnsHTTPTimeout, Transport: transport}
}

// ddnsURL asks each URL in turn until one answers with an address.
func ddnsURL(ctx context.Context, urls []string, v6 bool, bindIface string) (netip.Addr, string, error) {
	d, err := ddnsDialer(bindIface, v6, false)
	if err != nil {
		return netip.Addr{}, "", err
	}
	client := ddnsHTTPClient(d, v6)
	var last *ddnsError
	var notes []string
	for _, u := range urls {
		addr, err := ddnsFetch(ctx, client, u, v6)
		if err == nil {
			return addr, u, nil
		}
		last = err
		host := u
		if parsed, perr := url.Parse(u); perr == nil {
			host = parsed.Host
		}
		note := host + ": " + err.code
		if err.detail != "" {
			note += " (" + err.detail + ")"
		}
		notes = append(notes, note)
		if ctx.Err() != nil {
			break
		}
	}
	if last == nil {
		return netip.Addr{}, "", ddnsFail("no_address", "no URL")
	}
	return netip.Addr{}, "", ddnsFail(last.code, ddnsClip(strings.Join(notes, "; ")))
}

func ddnsFetch(ctx context.Context, client *http.Client, u string, v6 bool) (netip.Addr, *ddnsError) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return netip.Addr{}, ddnsFail("http_error", "bad URL")
	}
	// Echo services answer curl with the bare address.
	req.Header.Set("User-Agent", "curl/8.4.0")
	resp, err := client.Do(req)
	if err != nil {
		return netip.Addr{}, ddnsNetError(err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, ddnsBodyLimit))
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return netip.Addr{}, ddnsFail("http_error", fmt.Sprintf("HTTP %d", resp.StatusCode))
	}
	if err != nil && len(body) == 0 {
		return netip.Addr{}, ddnsNetError(err)
	}
	addr, ok := ddnsParse(string(body), v6)
	if !ok {
		return netip.Addr{}, ddnsFail("parse_error", "no "+ddnsFamilyName(v6)+" address in the response")
	}
	return addr, nil
}

// ddnsCmd runs command the way exec does, and takes the first address it prints.
func ddnsCmd(ctx context.Context, command string, v6 bool) (netip.Addr, error) {
	params, _ := json.Marshal(ExecParams{Command: command, Timeout: ddnsCmdTimeout})
	r, err := Exec(ctx, params)
	switch {
	case err != nil && err.Error() == "unsupported_platform":
		return netip.Addr{}, ddnsFail("unsupported", "commands do not run on this platform")
	case err != nil:
		return netip.Addr{}, ddnsFail("parse_error", err.Error())
	case r.TimedOut:
		return netip.Addr{}, ddnsFail("timeout", fmt.Sprintf("the command ran over %d seconds", ddnsCmdTimeout))
	case r.ExitCode != 0:
		detail := fmt.Sprintf("exit code %d", r.ExitCode)
		if line, _, _ := strings.Cut(strings.TrimSpace(r.Stderr), "\n"); line != "" {
			detail += ": " + line
		}
		return netip.Addr{}, ddnsFail("parse_error", ddnsClip(detail))
	}
	addr, ok := ddnsParse(r.Stdout, v6)
	if !ok {
		return netip.Addr{}, ddnsFail("parse_error", "no "+ddnsFamilyName(v6)+" address in the output")
	}
	return addr, nil
}

// ddnsIfaceAddrs is ifi's addresses, IPv4 unmapped, loopback left out.
func ddnsIfaceAddrs(ifi *net.Interface) []netip.Addr {
	raw, _ := ifi.Addrs()
	var out []netip.Addr
	for _, a := range raw {
		var ip net.IP
		switch a := a.(type) {
		case *net.IPNet:
			ip = a.IP
		case *net.IPAddr:
			ip = a.IP
		}
		addr, ok := netip.AddrFromSlice(ip)
		if !ok || addr.IsLoopback() {
			continue
		}
		out = append(out, addr.Unmap())
	}
	return out
}

// ddnsNICv4 orders a NIC's IPv4 addresses public first; link-local ones
// are left out, private ones are kept.
func ddnsNICv4(addrs []netip.Addr) []netip.Addr {
	var public, private []netip.Addr
	for _, addr := range addrs {
		if !addr.Is4() || !ddnsUsable(addr, false) || addr.IsLinkLocalUnicast() {
			continue
		}
		if addr.IsPrivate() || ddnsCGNAT.Contains(addr) {
			private = append(private, addr)
		} else {
			public = append(public, addr)
		}
	}
	return append(public, private...)
}

// ddnsAddrKey is one address on the interface with this index.
type ddnsAddrKey struct {
	index int
	addr  netip.Addr
}

type ddnsAddrFlags struct{ temporary, deprecated bool }

type ddnsCandidate struct {
	addr                  netip.Addr
	temporary, deprecated bool
}

func ddnsV6Candidates(ifi *net.Interface, flags map[ddnsAddrKey]ddnsAddrFlags) []ddnsCandidate {
	var out []ddnsCandidate
	for _, addr := range ddnsIfaceAddrs(ifi) {
		if !addr.Is6() {
			continue
		}
		f := flags[ddnsAddrKey{ifi.Index, addr}]
		out = append(out, ddnsCandidate{addr, f.temporary, f.deprecated})
	}
	return out
}

// ddnsPickV6 applies the NIC rules, then sel: global unicast only, never
// deprecated, temporary only when sel allows it, ULA only when allowPrivate.
func ddnsPickV6(list []ddnsCandidate, sel ddnsSelect, allowPrivate bool) (netip.Addr, bool) {
	var usable []netip.Addr
	for _, c := range list {
		if !ddnsUsable(c.addr, true) || c.deprecated || c.temporary && !sel.allowTemporary || c.addr.IsPrivate() && !allowPrivate {
			continue
		}
		usable = append(usable, c.addr)
	}
	if sel.mode == "index" {
		if sel.index <= len(usable) {
			return usable[sel.index-1], true
		}
		return netip.Addr{}, false
	}
	for _, addr := range usable {
		text := addr.String()
		switch {
		case sel.mode == "prefix" && sel.prefix.IsValid() && !sel.prefix.Contains(addr),
			sel.mode == "prefix" && !sel.prefix.IsValid() && !strings.HasPrefix(text, sel.text),
			sel.mode == "regex" && !sel.re.MatchString(text):
			continue
		}
		return addr, true
	}
	return netip.Addr{}, false
}

// ddnsRouteIface names the interface the default route of a family leaves
// through. Connecting a UDP socket only looks the route up; nothing is sent.
func ddnsRouteIface(v6 bool) (string, error) {
	network, target := "udp4", "8.8.8.8:53"
	if v6 {
		network, target = "udp6", "[2001:4860:4860::8888]:53"
	}
	c, err := net.Dial(network, target)
	if err != nil {
		return "", err
	}
	local := c.LocalAddr().(*net.UDPAddr).AddrPort().Addr().Unmap()
	c.Close()
	list, err := net.Interfaces()
	if err != nil {
		return "", err
	}
	for _, ifi := range list {
		for _, addr := range ddnsIfaceAddrs(&ifi) {
			if addr == local.WithZone("") {
				return ifi.Name, nil
			}
		}
	}
	return "", errors.New("no interface has the route's source address")
}

func ddnsNIC(s ddnsSpec) (netip.Addr, string, error) {
	name := s.iface
	if name == "" {
		var err error
		if name, err = ddnsRouteIface(s.v6); err != nil {
			if name, err = ddnsRouteIface(!s.v6); err != nil {
				return netip.Addr{}, "", ddnsFail("iface_not_found", "no default route")
			}
		}
	}
	ifi, err := net.InterfaceByName(name)
	if err != nil {
		return netip.Addr{}, "", ddnsFail("iface_not_found", name)
	}
	if !s.v6 {
		list := ddnsNICv4(ddnsIfaceAddrs(ifi))
		if len(list) == 0 {
			return netip.Addr{}, "", ddnsFail("no_address", "no IPv4 address on "+ifi.Name)
		}
		return list[0], ifi.Name, nil
	}
	flags, err := ddnsV6Flags()
	if err != nil {
		return netip.Addr{}, "", ddnsFail("unsupported", "cannot tell temporary IPv6 addresses: "+ddnsClip(err.Error()))
	}
	addr, ok := ddnsPickV6(ddnsV6Candidates(ifi, flags), s.sel, s.allowPrivate)
	if !ok {
		return netip.Addr{}, "", ddnsFail("no_address", "no matching IPv6 address on "+ifi.Name)
	}
	return addr, ifi.Name, nil
}
