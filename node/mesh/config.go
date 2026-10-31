package mesh

import (
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"net"
	"net/netip"
	"runtime"
	"slices"
	"strconv"
	"strings"

	"openbear/node/mesh/sign"
	"openbear/node/mesh/wire"
)

// ConfigSchema is the newest config.json schema this data plane reads and
// ConfigSchemaMin the oldest; status.json reports them as configSchemas, and
// the controller writes the newest schema every node of a mesh reads
// (app/servers/compat.py CONFIG_SCHEMA). A new schema raises ConfigSchema;
// the old one may be dropped one release later at the earliest.
//
// Schema 2 came with signed control messages and end-to-end encryption: a
// node's entry carries its signing and end-to-end keys and a relay's config
// the mesh's relay links. Schema 1 is not read: a node is upgraded together
// with the whole mesh and waits for the controller's schema 2 config.
const (
	ConfigSchema    = 2
	ConfigSchemaMin = 2
)

// e2eOverhead is what end-to-end encryption adds to a packet: the outer
// IPv4 header (20) and the inner WireGuard data header (16) and tag (16).
const e2eOverhead = 52

// configSchemas is ConfigSchemaMin..ConfigSchema; tests stand in for another
// release by changing it.
var configSchemas = [2]int{ConfigSchemaMin, ConfigSchema}

// ConfigSchemas is the range of config.json schemas this data plane reads.
func ConfigSchemas() [2]int { return configSchemas }

// protocol is the mesh node-to-node protocol range this data plane speaks
// (package wire); tests stand in for another release by changing it.
var protocol = wire.Local()

// Config is config.json as the controller generates it for one node.
type Config struct {
	Schema   int               `json:"schema"`
	Revision int64             `json:"revision"`
	Network  Network           `json:"network"`
	Self     Self              `json:"self"`
	Nodes    []Node            `json:"nodes"`
	Links    []Link            `json:"links"`
	Entry    *Entry            `json:"entry,omitempty"`
	NextHops map[string]string `json:"nextHops,omitempty"`
	Rules    []Rule            `json:"rules"`
	Health   *Health           `json:"health,omitempty"`
	// Reflectors are relays' UDP host:port that tell a node its public
	// address, for reachability and punched links.
	Reflectors   []string      `json:"reflectors,omitempty"`
	Reachability *Reachability `json:"reachability,omitempty"`
	// Routing is how a relay chooses next hops to other relays.
	Routing *Routing `json:"routing,omitempty"`
	// RelayLinks are every link between two relays of the mesh, each pair
	// once; relays only. Routing takes no other link from advertisements.
	RelayLinks [][2]string `json:"relayLinks,omitempty"`

	// keys are this node's signing and end-to-end keys, which the engine
	// running the config gives it (keys.go); not in config.json.
	keys *nodeKeys
}

// Routing: mode dynamic (the default) follows measured link costs, static
// keeps nextHops; bias adds milliseconds to the cost of the link to a
// relay, either way.
type Routing struct {
	Mode string             `json:"mode,omitempty"`
	Bias map[string]float64 `json:"bias,omitempty"`
}

// Reachability lists STUN servers, host:port, asked besides the reflectors.
type Reachability struct {
	STUN []string `json:"stun,omitempty"`
}

type Network struct {
	ID            string       `json:"id"`
	Prefix        netip.Prefix `json:"prefix"`
	ControlPrefix netip.Prefix `json:"controlPrefix"`
	MTU           int          `json:"mtu"`
}

type Self struct {
	ID              string     `json:"id"`
	Name            string     `json:"name"`
	Role            string     `json:"role"`
	Address         netip.Addr `json:"address"`
	ControlAddress  netip.Addr `json:"controlAddress"`
	Listen          Listen     `json:"listen"`
	Gateway         *Gateway   `json:"gateway,omitempty"`
	UploadLimitMbps *float64   `json:"uploadLimitMbps"`
	Resources       string     `json:"resources,omitempty"`
	// GatewayRoutes are the gateway subnets this node's rules let it reach
	// or be reached from. Absent (an older controller): every announced
	// subnet is routed. Present, even empty: a member's peers carry only
	// these, and a relay installs only these into its OS (relays still
	// forward every subnet).
	GatewayRoutes *[]netip.Prefix `json:"gatewayRoutes,omitempty"`
}

type Listen struct {
	UDP int `json:"udp"`
	TCP int `json:"tcp"`
	TLS int `json:"tls"`
}

type Gateway struct {
	Subnets    []netip.Prefix `json:"subnets"`
	Masquerade bool           `json:"masquerade"`
}

type Node struct {
	ID             string     `json:"id"`
	Name           string     `json:"name"`
	Role           string     `json:"role"`
	Address        netip.Addr `json:"address"`
	ControlAddress netip.Addr `json:"controlAddress"`
	PublicKey      string     `json:"publicKey"`
	ControlKey     string     `json:"controlKey,omitempty"`
	// SignKey (Ed25519, base64) verifies what the node signs; E2EKey
	// (X25519, base64) is the static key of its end-to-end sessions. Every
	// node has both: a mesh is upgraded as a whole.
	SignKey string         `json:"signKey"`
	E2EKey  string         `json:"e2eKey"`
	Subnets []netip.Prefix `json:"subnets,omitempty"`
	Relays  []string       `json:"relays,omitempty"`
}

type Link struct {
	Peer     string   `json:"peer"`
	Kind     string   `json:"kind"`
	Dial     bool     `json:"dial"`
	Endpoint string   `json:"endpoint,omitempty"`
	TLSPins  []string `json:"tlsPins,omitempty"`
	// Punch is a UDP link, direct or between relays, both ends open through
	// their NATs; Dial means nothing for it and Endpoint is optional.
	Punch bool `json:"punch,omitempty"`
}

type Entry struct {
	Mode       string      `json:"mode"`
	Candidates []Candidate `json:"candidates"`
	AutoReturn bool        `json:"autoReturn"`
}

type Candidate struct {
	ID       string   `json:"id"`
	Relay    string   `json:"relay"`
	Endpoint string   `json:"endpoint"`
	TLSPins  []string `json:"tlsPins,omitempty"`
	// Punch is a relay behind NAT: the member and the relay open a UDP
	// path, signalled through another candidate's relay; Endpoint is
	// optional and one more address to try.
	Punch bool `json:"punch,omitempty"`
}

// Rule allows new traffic into this node; replies to its own traffic are
// let back in without one. Empty Src matches any source, empty Dst any of
// this node's own addresses.
type Rule struct {
	Src   []netip.Prefix `json:"src"`
	Dst   []netip.Prefix `json:"dst"`
	Proto string         `json:"proto"`
	Ports [][2]uint16    `json:"ports,omitempty"`
}

// Health is read by the health package; the static data plane only checks it.
type Health struct {
	ActiveIntervalMs    int `json:"activeIntervalMs"`
	StandbyIntervalMs   int `json:"standbyIntervalMs"`
	ColdRetryMs         int `json:"coldRetryMs"`
	InitialTimeoutMs    int `json:"initialTimeoutMs"`
	MinTimeoutMs        int `json:"minTimeoutMs"`
	MaxTimeoutMs        int `json:"maxTimeoutMs"`
	ConsecutiveFailures int `json:"consecutiveFailures"`
	ReadySuccesses      int `json:"readySuccesses"`
	HandshakeGraceMs    int `json:"handshakeGraceMs"`
	ReturnHealthyMs     int `json:"returnHealthyMs"`
	MinResidenceMs      int `json:"minResidenceMs"`
	LeaseMs             int `json:"leaseMs"`
	LeaseRefreshMs      int `json:"leaseRefreshMs"`
}

const (
	roleMember = "member"
	roleRelay  = "relay"
	kindEntry  = "entry"
	kindRelay  = "relay"
	kindDirect = "direct"
	kindMember = "member"
)

// ParseConfig decodes and checks config.json. Unknown fields are allowed:
// incompatible changes bump the schema instead.
func ParseConfig(raw []byte) (*Config, error) {
	var head struct {
		Schema *int `json:"schema"`
	}
	if err := json.Unmarshal(raw, &head); err != nil {
		return nil, fmt.Errorf("invalid config JSON: %w", err)
	}
	if head.Schema == nil {
		return nil, errors.New("config has no schema")
	}
	if s := configSchemas; *head.Schema < s[0] || *head.Schema > s[1] {
		return nil, fmt.Errorf("unsupported config schema %d (this data plane reads %d..%d)", *head.Schema, s[0], s[1])
	}
	var c Config
	if err := json.Unmarshal(raw, &c); err != nil {
		return nil, fmt.Errorf("invalid config: %w", err)
	}
	if err := c.check(); err != nil {
		return nil, err
	}
	return &c, nil
}

// Validate checks a config the node is about to write; the same checks Run applies.
func Validate(raw []byte) error {
	_, err := ParseConfig(raw)
	return err
}

func (c *Config) node(id string) *Node {
	for i := range c.Nodes {
		if c.Nodes[i].ID == id {
			return &c.Nodes[i]
		}
	}
	return nil
}

func (c *Config) isRelay() bool { return c.Self.Role == roleRelay }

// roleOf answers for this node too.
func (c *Config) roleOf(id string) string {
	if id == c.Self.ID {
		return c.Self.Role
	}
	if n := c.node(id); n != nil {
		return n.Role
	}
	return ""
}

func (c *Config) check() error {
	if c.Revision < 0 {
		return errors.New("revision must not be negative")
	}
	n := c.Network
	if err := checkSubnet("network.prefix", n.Prefix); err != nil {
		return err
	}
	if n.Prefix.Bits() > 30 {
		return errors.New("network.prefix is too small")
	}
	if err := checkSubnet("network.controlPrefix", n.ControlPrefix); err != nil {
		return err
	}
	if n.ControlPrefix.Bits() <= n.Prefix.Bits() || !n.Prefix.Contains(n.ControlPrefix.Addr()) {
		return errors.New("network.controlPrefix must be a part of network.prefix")
	}
	if n.MTU < 576+e2eOverhead || n.MTU > 1420 {
		return fmt.Errorf("network.mtu must be between %d and 1420", 576+e2eOverhead)
	}

	s := c.Self
	if s.ID == "" {
		return errors.New("self.id is empty")
	}
	if s.Role != roleMember && s.Role != roleRelay {
		return fmt.Errorf("self.role %q is neither member nor relay", s.Role)
	}
	if s.Role == roleRelay && runtime.GOOS != "linux" {
		return errors.New("only Linux nodes can be relays")
	}
	used := map[netip.Addr]string{}
	if err := c.checkAddresses("self", s.Address, s.ControlAddress, used); err != nil {
		return err
	}
	for name, p := range map[string]int{"udp": s.Listen.UDP, "tcp": s.Listen.TCP, "tls": s.Listen.TLS} {
		if p < 0 || p > 65535 {
			return fmt.Errorf("self.listen.%s is not a port", name)
		}
	}
	if s.Listen.TCP != 0 && s.Listen.TCP == s.Listen.TLS {
		return errors.New("self.listen.tcp and self.listen.tls are the same port")
	}
	if s.Listen.TLS != 0 && s.Role != roleRelay {
		return errors.New("self.listen.tls is for relays; this node is a member")
	}
	if s.UploadLimitMbps != nil && (*s.UploadLimitMbps <= 0 || *s.UploadLimitMbps > 100000) {
		return errors.New("self.uploadLimitMbps must be positive and at most 100000")
	}
	if s.Resources != "" && s.Resources != roleMember && s.Resources != roleRelay {
		return fmt.Errorf("self.resources %q is neither member nor relay", s.Resources)
	}
	var announced []netip.Prefix
	if s.Gateway != nil {
		if runtime.GOOS != "linux" {
			return errors.New("only Linux nodes can be gateways")
		}
		if len(s.Gateway.Subnets) == 0 {
			return errors.New("self.gateway has no subnets")
		}
		for i, p := range s.Gateway.Subnets {
			if err := c.checkAnnounced(fmt.Sprintf("self.gateway.subnets[%d]", i), p, announced); err != nil {
				return err
			}
			announced = append(announced, p)
		}
	}

	keys := map[string]string{}
	ids := map[string]bool{s.ID: true}
	for i := range c.Nodes {
		nd := &c.Nodes[i]
		at := fmt.Sprintf("nodes[%d]", i)
		if nd.ID == "" || ids[nd.ID] {
			return fmt.Errorf("%s.id is empty or repeated", at)
		}
		ids[nd.ID] = true
		if nd.Role != roleMember && nd.Role != roleRelay {
			return fmt.Errorf("%s.role %q is neither member nor relay", at, nd.Role)
		}
		if err := c.checkAddresses(at, nd.Address, nd.ControlAddress, used); err != nil {
			return err
		}
		k, err := keyHex(nd.PublicKey)
		if err != nil {
			return fmt.Errorf("%s.publicKey: %w", at, err)
		}
		if other, ok := keys[k]; ok {
			return fmt.Errorf("%s.publicKey is also %s's", at, other)
		}
		keys[k] = nd.ID
		if nd.ControlKey != "" {
			if _, err := keyHex(nd.ControlKey); err != nil {
				return fmt.Errorf("%s.controlKey: %w", at, err)
			}
		}
		if _, err := sign.ParsePublic(nd.SignKey); err != nil {
			return fmt.Errorf("%s.signKey: %w", at, err)
		}
		ek, err := keyHex(nd.E2EKey)
		if err != nil {
			return fmt.Errorf("%s.e2eKey: %w", at, err)
		}
		if other, ok := keys[ek]; ok {
			return fmt.Errorf("%s.e2eKey is also %s's", at, other)
		}
		keys[ek] = nd.ID
		for j, p := range nd.Subnets {
			if err := c.checkAnnounced(fmt.Sprintf("%s.subnets[%d]", at, j), p, announced); err != nil {
				return err
			}
			announced = append(announced, p)
		}
		if nd.Role == roleRelay && len(nd.Relays) > 0 {
			return fmt.Errorf("%s is a relay but lists relays to attach to", at)
		}
	}
	for i, nd := range c.Nodes {
		seen := map[string]bool{}
		for j, r := range nd.Relays {
			if c.roleOf(r) != roleRelay || seen[r] {
				return fmt.Errorf("nodes[%d].relays[%d] %q is not a relay or is repeated", i, j, r)
			}
			seen[r] = true
		}
	}

	linked := map[string]bool{}
	for i, l := range c.Links {
		at := fmt.Sprintf("links[%d]", i)
		peer := c.node(l.Peer)
		if peer == nil {
			return fmt.Errorf("%s.peer %q is not in nodes", at, l.Peer)
		}
		if linked[l.Peer] {
			return fmt.Errorf("%s repeats peer %q", at, l.Peer)
		}
		linked[l.Peer] = true
		switch l.Kind {
		case kindRelay:
			if s.Role != roleRelay || peer.Role != roleRelay {
				return fmt.Errorf("%s: a relay link joins two relays", at)
			}
		case kindDirect:
			if s.Role != roleMember || peer.Role != roleMember {
				return fmt.Errorf("%s: a direct link joins two members", at)
			}
		default:
			return fmt.Errorf("%s.kind %q is neither relay nor direct", at, l.Kind)
		}
		if l.Punch {
			// Without self.listen.udp the punched path uses an ephemeral port.
			if l.Endpoint != "" {
				if err := checkDialed(at, l.Endpoint, l.TLSPins); err != nil {
					return err
				}
				if transportOf(l.Endpoint) != "udp" {
					return fmt.Errorf("%s is punched but its endpoint is not udp", at)
				}
			} else if len(l.TLSPins) > 0 {
				return fmt.Errorf("%s is punched but has tlsPins", at)
			}
		} else if l.Dial {
			if err := checkDialed(at, l.Endpoint, l.TLSPins); err != nil {
				return err
			}
		} else if l.Endpoint != "" || len(l.TLSPins) > 0 {
			return fmt.Errorf("%s has an endpoint or tlsPins but does not dial", at)
		}
	}

	if c.Entry != nil {
		if s.Role != roleMember {
			return errors.New("entry is for members; this node is a relay")
		}
		if err := c.checkEntry(linked); err != nil {
			return err
		}
	}
	if len(c.NextHops) > 0 && s.Role != roleRelay {
		return errors.New("nextHops is for relays; this node is a member")
	}
	for to, via := range c.NextHops {
		if to == s.ID || c.roleOf(to) != roleRelay {
			return fmt.Errorf("nextHops: %q is not another relay", to)
		}
		if !linked[via] || c.roleOf(via) != roleRelay {
			return fmt.Errorf("nextHops[%s]: %q is not a linked relay", to, via)
		}
	}
	if s.GatewayRoutes != nil {
		if len(*s.GatewayRoutes) > 4096 {
			return errors.New("self.gatewayRoutes has more than 4096 prefixes")
		}
		for i, p := range *s.GatewayRoutes {
			at := fmt.Sprintf("self.gatewayRoutes[%d]", i)
			if err := checkSubnet(at, p); err != nil {
				return err
			}
			if p.Overlaps(n.Prefix) {
				return fmt.Errorf("%s %v overlaps network.prefix", at, p)
			}
		}
	}
	if err := c.checkRelayLinks(linked); err != nil {
		return err
	}
	if r := c.Routing; r != nil {
		if s.Role != roleRelay {
			return errors.New("routing is for relays; this node is a member")
		}
		if r.Mode != "" && r.Mode != "dynamic" && r.Mode != "static" {
			return fmt.Errorf("routing.mode %q is neither dynamic nor static", r.Mode)
		}
		for peer, b := range r.Bias {
			if !linked[peer] || c.roleOf(peer) != roleRelay {
				return fmt.Errorf("routing.bias: %q is not a linked relay", peer)
			}
			if math.IsNaN(b) || b < -10000 || b > 10000 {
				return fmt.Errorf("routing.bias[%s] must be between -10000 and 10000 ms", peer)
			}
		}
	}
	if err := checkObservers("reflectors", c.Reflectors); err != nil {
		return err
	}
	if c.Reachability != nil {
		if err := checkObservers("reachability.stun", c.Reachability.STUN); err != nil {
			return err
		}
	}
	for i, r := range c.Rules {
		if err := checkRule(r); err != nil {
			return fmt.Errorf("rules[%d]: %w", i, err)
		}
	}
	if h := c.Health; h != nil {
		for _, v := range []int{h.ActiveIntervalMs, h.StandbyIntervalMs, h.ColdRetryMs, h.InitialTimeoutMs, h.MinTimeoutMs, h.MaxTimeoutMs,
			h.ConsecutiveFailures, h.ReadySuccesses, h.HandshakeGraceMs, h.ReturnHealthyMs, h.MinResidenceMs, h.LeaseMs, h.LeaseRefreshMs} {
			if v < 0 {
				return errors.New("health values must not be negative")
			}
		}
		if err := healthConfig(c).Parameters.Validate(); err != nil {
			return err
		}
	}
	return nil
}

// checkRelayLinks: relayLinks name relays, each pair once, and hold every
// relay link of this one.
func (c *Config) checkRelayLinks(linked map[string]bool) error {
	if len(c.RelayLinks) > 0 && !c.isRelay() {
		return errors.New("relayLinks is for relays; this node is a member")
	}
	if len(c.RelayLinks) > 4096 {
		return errors.New("relayLinks has more than 4096 links")
	}
	pairs := map[[2]string]bool{}
	for i, p := range c.RelayLinks {
		a, b := p[0], p[1]
		if b < a {
			a, b = b, a
		}
		if a == b || c.roleOf(a) != roleRelay || c.roleOf(b) != roleRelay || pairs[[2]string{a, b}] {
			return fmt.Errorf("relayLinks[%d] is not two relays, or is repeated", i)
		}
		pairs[[2]string{a, b}] = true
	}
	if !c.isRelay() {
		return nil
	}
	for peer := range linked {
		if c.roleOf(peer) != roleRelay {
			continue
		}
		a, b := c.Self.ID, peer
		if b < a {
			a, b = b, a
		}
		if !pairs[[2]string{a, b}] {
			return fmt.Errorf("relayLinks lacks this relay's link to %q", peer)
		}
	}
	return nil
}

func checkObservers(at string, list []string) error {
	if len(list) > 16 {
		return fmt.Errorf("at most 16 %s are supported", at)
	}
	for i, r := range list {
		host, port, err := net.SplitHostPort(r)
		if p, perr := strconv.Atoi(port); err != nil || perr != nil || host == "" || p < 1 || p > 65535 {
			return fmt.Errorf("%s[%d] %q is not host:port", at, i, r)
		}
	}
	return nil
}

func (c *Config) checkEntry(linked map[string]bool) error {
	e := c.Entry
	if len(e.Candidates) > 8 {
		return errors.New("at most eight entry candidates are supported")
	}
	if e.Mode != "fixed" && e.Mode != "failover" {
		return fmt.Errorf("entry.mode %q is neither fixed nor failover", e.Mode)
	}
	ids := map[string]bool{}
	for i, cand := range e.Candidates {
		at := fmt.Sprintf("entry.candidates[%d]", i)
		if cand.ID == "" || ids[cand.ID] {
			return fmt.Errorf("%s.id is empty or repeated", at)
		}
		ids[cand.ID] = true
		if c.roleOf(cand.Relay) != roleRelay || cand.Relay == c.Self.ID {
			return fmt.Errorf("%s.relay %q is not a relay", at, cand.Relay)
		}
		if linked[cand.Relay] {
			return fmt.Errorf("%s.relay %q is also a link", at, cand.Relay)
		}
		if cand.Punch {
			// An endpoint, if any, is one more address to punch toward.
			if len(cand.TLSPins) > 0 {
				return fmt.Errorf("%s is punched but has tlsPins", at)
			}
			if cand.Endpoint != "" {
				if err := checkDialed(at, cand.Endpoint, nil); err != nil {
					return err
				}
				if transportOf(cand.Endpoint) != "udp" {
					return fmt.Errorf("%s is punched but its endpoint is not udp", at)
				}
			}
			continue
		}
		if err := checkDialed(at, cand.Endpoint, cand.TLSPins); err != nil {
			return err
		}
	}
	return nil
}

// checkDialed checks an endpoint this node connects to and its TLS pins.
func checkDialed(at, endpoint string, pins []string) error {
	ep, err := parseEndpoint(endpoint)
	if err != nil {
		return fmt.Errorf("%s.endpoint: %w", at, err)
	}
	if len(pins) > 2 {
		return fmt.Errorf("%s has more than two tlsPins", at)
	}
	if ep.scheme == "tls" && len(pins) == 0 {
		return fmt.Errorf("%s is tls without tlsPins", at)
	}
	if ep.scheme != "tls" && len(pins) > 0 {
		return fmt.Errorf("%s has tlsPins but is not tls", at)
	}
	for _, pin := range pins {
		if b, err := hex.DecodeString(pin); err != nil || len(b) != 32 {
			return fmt.Errorf("%s has a tlsPin that is not a SHA-256 hex", at)
		}
	}
	return nil
}

func (c *Config) checkAddresses(at string, addr, control netip.Addr, used map[netip.Addr]string) error {
	if !addr.Is4() || !c.Network.Prefix.Contains(addr) || c.Network.ControlPrefix.Contains(addr) {
		return fmt.Errorf("%s.address %v is not in network.prefix outside the control prefix", at, addr)
	}
	if !control.Is4() || !c.Network.ControlPrefix.Contains(control) {
		return fmt.Errorf("%s.controlAddress %v is not in network.controlPrefix", at, control)
	}
	for _, a := range []netip.Addr{addr, control} {
		if a == c.Network.Prefix.Addr() || a == c.Network.ControlPrefix.Addr() {
			return fmt.Errorf("%s uses a network address %v", at, a)
		}
		if other, ok := used[a]; ok {
			return fmt.Errorf("%s reuses %v of %s", at, a, other)
		}
		used[a] = at
	}
	return nil
}

func (c *Config) checkAnnounced(at string, p netip.Prefix, announced []netip.Prefix) error {
	if err := checkSubnet(at, p); err != nil {
		return err
	}
	if p.Overlaps(c.Network.Prefix) {
		return fmt.Errorf("%s %v overlaps network.prefix", at, p)
	}
	for _, q := range announced {
		if p.Overlaps(q) {
			return fmt.Errorf("%s %v overlaps another gateway's %v", at, p, q)
		}
	}
	return nil
}

func checkSubnet(at string, p netip.Prefix) error {
	if !p.IsValid() || !p.Addr().Is4() {
		return fmt.Errorf("%s is not an IPv4 prefix", at)
	}
	if p.Masked() != p {
		return fmt.Errorf("%s %v has host bits set", at, p)
	}
	return nil
}

func checkRule(r Rule) error {
	for _, list := range [][]netip.Prefix{r.Src, r.Dst} {
		for _, p := range list {
			if err := checkSubnet("prefix", p); err != nil {
				return err
			}
		}
	}
	switch r.Proto {
	case "any", "tcp", "udp":
	case "icmp":
		if len(r.Ports) > 0 {
			return errors.New("icmp has no ports")
		}
	default:
		return fmt.Errorf("proto %q is not any, tcp, udp or icmp", r.Proto)
	}
	for _, pr := range r.Ports {
		if pr[0] == 0 || pr[0] > pr[1] {
			return fmt.Errorf("port range %d-%d is invalid", pr[0], pr[1])
		}
	}
	return nil
}

type endpoint struct {
	scheme, host string
	port         int
}

func parseEndpoint(s string) (endpoint, error) {
	scheme, rest, ok := strings.Cut(s, "://")
	if !ok || (scheme != "udp" && scheme != "tcp" && scheme != "tls") {
		return endpoint{}, fmt.Errorf("%q is not udp://, tcp:// or tls://host:port", s)
	}
	host, port, err := net.SplitHostPort(rest)
	if err != nil || host == "" {
		return endpoint{}, fmt.Errorf("%q has no host:port", s)
	}
	p, err := strconv.Atoi(port)
	if err != nil || p < 1 || p > 65535 {
		return endpoint{}, fmt.Errorf("%q has no valid port", s)
	}
	return endpoint{scheme, host, p}, nil
}

func keyHex(b64 string) (string, error) {
	k, err := base64.StdEncoding.DecodeString(strings.TrimSpace(b64))
	if err != nil || len(k) != 32 {
		return "", errors.New("not a base64 32-byte key")
	}
	return hex.EncodeToString(k), nil
}

func host(a netip.Addr) netip.Prefix { return netip.PrefixFrom(a, a.BitLen()) }

func sortPrefixes(list []netip.Prefix) []netip.Prefix {
	list = slices.Clone(list)
	slices.SortFunc(list, func(a, b netip.Prefix) int {
		if c := a.Addr().Compare(b.Addr()); c != 0 {
			return c
		}
		return a.Bits() - b.Bits()
	})
	return slices.Compact(list)
}
