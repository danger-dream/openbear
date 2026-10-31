package mesh

import (
	"encoding/json"
	"fmt"
	"math"
	"net/netip"
	"openbear/node/mesh/health"
	meshrouting "openbear/node/mesh/routing"
	"os"
	"path/filepath"
	"runtime"
	"runtime/metrics"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"

	"golang.zx2c4.com/wireguard/device"
)

// Version is the data plane's version in status.json; the node sets it to
// its own before calling Run.
var Version = "dev"

const statusSchema = 1

// relaySessionWindow is how recent a relay peer's handshake or
// authenticated packet has to be for its session to count as working:
// three rekey periods.
const relaySessionWindow = 3 * device.RekeyAfterTime

type Status struct {
	Schema          int                    `json:"schema"`
	PID             int                    `json:"pid"`
	Version         string                 `json:"version"`
	StartedAt       int64                  `json:"startedAt"`
	ConfigRevision  int64                  `json:"configRevision"`
	AppliedRevision int64                  `json:"appliedRevision"`
	Error           string                 `json:"error"`
	Ready           bool                   `json:"ready"`
	Iface           string                 `json:"iface"`
	TLSCertificate  string                 `json:"tlsCertificate"`
	Peers           []PeerStatus           `json:"peers"`
	Entry           *EntryStatus           `json:"entry,omitempty"`
	Owners          map[string]OwnerStatus `json:"owners,omitempty"`
	// SkippedRoutes overlap a network of this machine and are not routed.
	SkippedRoutes []string `json:"skippedRoutes"`
	// GatewayRoutes are the gateway subnets routed into the mesh in this
	// machine's OS: those of self.gatewayRoutes (all without it) that some
	// peer carries, less SkippedRoutes.
	GatewayRoutes []string `json:"gatewayRoutes"`
	// HostFirewall is "on" when the macOS application firewall is enabled;
	// on Linux the host firewalls found, comma-separated: ufw or iptables,
	// docker, firewalld, nftables.
	HostFirewall string `json:"hostFirewall"`
	// HostFirewallIssues is what the host firewall may not let through
	// (Linux; hostfw_linux.go).
	HostFirewallIssues []FirewallIssue `json:"hostFirewallIssues,omitempty"`
	Counters           CounterStatus   `json:"counters"`
	Resources          ResourceStatus  `json:"resources"`
	Events             []Event         `json:"events"`
	// Reachability is how this node's UDP socket looks from outside.
	Reachability *ReachabilityStatus `json:"reachability,omitempty"`
	// Routing is a relay's links to other relays and its next hops.
	Routing *RoutingStatus `json:"routing,omitempty"`
	// E2E is this node's end-to-end sessions (e2e.go).
	E2E *E2EStatus `json:"e2e,omitempty"`
	// Security counts the signatures this node checked.
	Security *SecurityStatus `json:"security,omitempty"`
	// ConfigStamp is the modification time, in Unix nanoseconds, of the
	// config.json that ConfigRevision and Error are about: the node tells by
	// it that the data plane read what it wrote, also when it wrote the same
	// revision again.
	ConfigStamp int64 `json:"configStamp,omitempty"`
	// ConfigSchemas is [min, max] of the config.json schemas this data plane
	// reads. A status.json without it is from a data plane that reads only
	// schema 1.
	ConfigSchemas [2]int `json:"configSchemas"`
	// Protocols is [min, max] of the mesh node-to-node protocols it speaks.
	Protocols [2]int `json:"protocols"`
}

// RoutingStatus: a cost is null where it is infinite, for a link down or
// suppressed and a destination without a route.
type RoutingStatus struct {
	Mode   string       `json:"mode"`
	Links  []RouteLink  `json:"links"`
	Routes []RelayRoute `json:"routes"`
}

type RouteLink struct {
	Peer     string   `json:"peer"`
	State    string   `json:"state"` // up, down, suppressed
	RTTMs    float64  `json:"rttMs"`
	JitterMs float64  `json:"jitterMs"`
	Loss     float64  `json:"loss"`
	Cost     *float64 `json:"cost"`
	Penalty  float64  `json:"penalty"`
}

type RelayRoute struct {
	Dest     string   `json:"dest"`
	NextHop  string   `json:"nextHop"`
	Cost     *float64 `json:"cost"`
	Path     []string `json:"path"`
	Since    int64    `json:"since"`
	Switches int      `json:"switches"`
}

func finite(v float64) *float64 {
	if math.IsInf(v, 0) || math.IsNaN(v) {
		return nil
	}
	return &v
}

func routingStatus(r *meshrouting.Router) *RoutingStatus {
	st := r.Status()
	out := &RoutingStatus{Mode: st.Mode, Links: []RouteLink{}, Routes: []RelayRoute{}}
	for _, l := range st.Links {
		out.Links = append(out.Links, RouteLink{Peer: l.Peer, State: l.State, RTTMs: l.RTTMs, JitterMs: l.JitterMs, Loss: math.Round(l.Loss*1000) / 1000, Cost: finite(l.Cost), Penalty: l.Penalty})
	}
	for _, x := range st.Routes {
		out.Routes = append(out.Routes, RelayRoute{Dest: x.Dest, NextHop: x.NextHop, Cost: finite(x.Cost), Path: x.Path, Since: x.Since.UnixMilli(), Switches: x.Switches})
	}
	return out
}

// SecurityStatus: Signing counts the signatures checked of what nodes say
// on another's behalf, the rejected by the peer they came from; Claims the
// stored ownership claims dropped since start for want of valid ones.
type SecurityStatus struct {
	Signing SigningStatus `json:"signing"`
	Claims  ClaimsStatus  `json:"claims"`
}

type SigningStatus struct {
	Verified uint64            `json:"verified"`
	Rejected uint64            `json:"rejected"`
	ByHop    map[string]uint64 `json:"byHop,omitempty"`
}

type ClaimsStatus struct {
	Expired int `json:"expired"`
}

type ReachabilityStatus struct {
	CheckedAt  int64            `json:"checkedAt"`
	Verdict    string           `json:"verdict"` // public, punchable, symmetric, no_udp, unknown
	PublicAddr string           `json:"publicAddr"`
	Consistent bool             `json:"consistent"`
	Mapping    string           `json:"mapping"`   // stable, varies, unknown
	Filtering  string           `json:"filtering"` // open, address, port, unknown
	Observers  []ObserverStatus `json:"observers"`
}

type ObserverStatus struct {
	ID   string `json:"id"`
	Kind string `json:"kind"` // relay or stun
	Addr string `json:"addr"`
	OK   bool   `json:"ok"`
}

type PeerStatus struct {
	ID   string `json:"id"`
	Kind string `json:"kind"`
	// Active: for a relay, its session works (a handshake or an
	// authenticated packet within relaySessionWindow); for other peers,
	// it carries destinations.
	Active bool `json:"active"`
	// Routed says that some destination goes to this peer.
	Routed    bool   `json:"routed"`
	Transport string `json:"transport"`
	// Endpoint is where the path in use goes, as configured (a host name
	// stays one) or, for a path the peer opened, where it comes from;
	// RemoteAddr the IP and port that is, and LocalAddr this end's (for
	// the UDP socket, which is bound to every address, the one the system
	// sends from to RemoteAddr). All three are empty without a path in
	// use.
	Endpoint   string `json:"endpoint"`
	LocalAddr  string `json:"localAddr"`
	RemoteAddr string `json:"remoteAddr"`
	// LastHandshakeUnixNano is a string: JSON numbers lose nanoseconds.
	LastHandshakeUnixNano string `json:"lastHandshakeUnixNano"`
	// Handshakes counts handshake times this process saw change, a lower bound.
	Handshakes  int     `json:"handshakes"`
	RxBytes     uint64  `json:"rxBytes"`
	TxBytes     uint64  `json:"txBytes"`
	RTTMs       float64 `json:"rttMs"`
	TimeoutRate float64 `json:"timeoutRate"`
	// Reconnects counts TCP and TLS connections after the first since start.
	Reconnects int `json:"reconnects"`
	// Direct is the state of a punched link.
	Direct *DirectStatus `json:"direct,omitempty"`
	// Protocol is the mesh node-to-node protocol control messages to this
	// peer go out in, once its range is known from what it sent.
	Protocol int `json:"protocol,omitempty"`
	// Redial: a link this node dials over TCP or TLS (health/redial.go).
	Redial *RedialStatus `json:"redial,omitempty"`
}

// RedialStatus is what status shows of redialing a TCP or TLS link or entry
// candidate this node dials when it stays degraded. BaselineMs is null
// until enough minutes are measured; Level the redials since it was last
// healthy for ten minutes (0 to 4: the next waits 10, 10, 20, 40 or 60
// minutes after the last); Redials counts them since start, failed ones
// too; LastAt and NextAt (ms, 0 before the first) the last one and the
// earliest the next may be; DegradedMinutes the bad minutes in a row up
// to now (three redial).
type RedialStatus struct {
	BaselineMs      *float64 `json:"baselineMs"`
	Level           int      `json:"level"`
	Redials         int      `json:"redials"`
	LastAt          int64    `json:"lastAt"`
	NextAt          int64    `json:"nextAt"`
	DegradedMinutes int      `json:"degradedMinutes"`
}

func redialStatus(r *health.RedialStatus) *RedialStatus {
	if r == nil {
		return nil
	}
	out := &RedialStatus{Level: r.Level, Redials: r.Redials, DegradedMinutes: r.Degraded}
	if r.Baseline > 0 {
		ms := float64(r.Baseline) / float64(time.Millisecond)
		out.BaselineMs = &ms
	}
	if !r.Last.IsZero() {
		out.LastAt = r.Last.UnixMilli()
		out.NextAt = r.Next.UnixMilli()
	}
	return out
}

type DirectStatus struct {
	State     string `json:"state"` // direct, punching or relayed
	Via       string `json:"via"`   // lan, public, configured; empty unless direct
	Since     int64  `json:"since"`
	Attempts  int    `json:"attempts"`
	LastError string `json:"lastError"`
}

type EntryStatus struct {
	Candidate string `json:"candidate"`
	Relay     string `json:"relay"`
	Transport string `json:"transport"`
	Since     int64  `json:"since"`
	Switches  int    `json:"switches"`
	// Candidates is every entry candidate, standbys included.
	Candidates []CandidateHealth `json:"candidates"`
}

type CandidateHealth struct {
	ID        string `json:"id"`
	Relay     string `json:"relay"`
	Transport string `json:"transport"`
	State     string `json:"state"` // active, ready, failing, punching, untested
	Since     int64  `json:"since"`
	LastOKAt  int64  `json:"lastOkAt"`
	LastError string `json:"lastError"`
	// Via: how a punched candidate's endpoint was found, lan, public or
	// configured as for direct links; empty for others and until then.
	Via string `json:"via"`
	// What its probes measured; a cold candidate's are those of its
	// checks (health.Measurement). RTTMs is the median of the last five
	// answers and LossRate the share of probes timed out, both within the
	// minute up to the last outcome; null without one there, RTTMs also
	// when none was answered. CheckedAt is that last outcome, 0 before
	// one. Probes and Timeouts count since start, a failed attempt to
	// connect as one of each.
	RTTMs     *float64 `json:"rttMs"`
	LossRate  *float64 `json:"lossRate"`
	CheckedAt int64    `json:"checkedAt"`
	Probes    uint64   `json:"probes"`
	Timeouts  uint64   `json:"timeouts"`
	// Redial: a TCP or TLS candidate (RedialStatus).
	Redial *RedialStatus `json:"redial,omitempty"`
}

type OwnerStatus struct {
	Relay   string `json:"relay"`
	Seq     uint64 `json:"seq"`
	LeaseMs int64  `json:"leaseMs"`
}

type CounterStatus struct {
	Delivered       uint64 `json:"delivered"`
	Forwarded       uint64 `json:"forwarded"`
	DroppedACL      uint64 `json:"droppedAcl"`
	DroppedNotRelay uint64 `json:"droppedNotRelay"`
	DroppedTTL      uint64 `json:"droppedTtl"`
	DroppedNoRoute  uint64 `json:"droppedNoRoute"`
	DroppedQueue    uint64 `json:"droppedQueue"`
	DroppedBad      uint64 `json:"droppedBad"`
	// DroppedPlain: what a relay would have forwarded that is not end to
	// end, and plain packets for this node not from their peer's node.
	DroppedPlain uint64 `json:"droppedPlain"`
}

type ResourceStatus struct {
	Profile        string `json:"profile,omitempty"`
	PendingProfile string `json:"pendingProfile,omitempty"`
	RSSBytes       uint64 `json:"rssBytes"`
	HeapBytes      uint64 `json:"heapBytes"`
	QueueBytes     int    `json:"queueBytes"`
	Conns          int    `json:"conns"`
	Goroutines     int    `json:"goroutines"`
	// CPUPercent is of one core over the last status interval.
	CPUPercent float64 `json:"cpuPercent"`
}

type Event struct {
	Seq    uint64 `json:"seq"`
	At     int64  `json:"at"`
	Kind   string `json:"kind"`
	Detail string `json:"detail"`
}

const maxEvents = 100

type eventLog struct {
	mu   sync.Mutex
	seq  uint64
	list []Event
}

func (l *eventLog) add(kind, detail string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.seq++
	l.list = append(l.list, Event{Seq: l.seq, At: time.Now().UnixMilli(), Kind: kind, Detail: detail})
	if len(l.list) > maxEvents {
		l.list = append(l.list[:0], l.list[len(l.list)-maxEvents:]...)
	}
}

func (l *eventLog) snapshot() []Event {
	l.mu.Lock()
	defer l.mu.Unlock()
	return append([]Event{}, l.list...)
}

// status reports the engine; the caller fills revisions and error.
func (e *engine) status() Status {
	e.mu.Lock()
	defer e.mu.Unlock()
	_, state := deviceState(e.dev)
	local := e.bind.localAddrs()
	s := Status{Iface: e.name, TLSCertificate: e.bind.certificate(), Peers: []PeerStatus{}}
	s.Resources.Profile = e.resourceStart
	if e.resourceStart != "" && resourceProfile(e.cfg) != e.resourceStart {
		s.Resources.PendingProfile = resourceProfile(e.cfg)
	}
	handshaken := false
	for _, p := range e.peers {
		ps := PeerStatus{ID: p.ID, Kind: p.Kind, Active: len(p.Allowed) > 0, Routed: len(p.Allowed) > 0, Transport: transportOf(p.Endpoint), LastHandshakeUnixNano: "0"}
		if p.Kind == kindRelay {
			// A relay forwards whatever reaches it whether or not a
			// destination goes to it here (meshTUN.AuthorizeSource).
			st := state[p.Key]
			ps.Active = st != nil && st.handshakeNano > 0 && time.Since(time.Unix(0, st.handshakeNano)) < relaySessionWindow
		}
		if st := state[p.Key]; st != nil {
			if st.endpoint != "" {
				ps.Transport, ps.Endpoint = transportOf(st.endpoint), stripScheme(st.endpoint)
				if _, err := netip.ParseAddrPort(ps.Endpoint); err == nil {
					ps.RemoteAddr = ps.Endpoint
				}
				ps.LocalAddr = localAddrOf(local[st.endpoint], ps.RemoteAddr)
			}
			ps.RxBytes, ps.TxBytes = st.rx, st.tx
			if st.handshakeNano > 0 {
				handshaken = true
				ps.LastHandshakeUnixNano = strconv.FormatInt(st.handshakeNano, 10)
				h := e.hs[p.Key]
				if h == nil {
					h = &handshakes{}
					e.hs[p.Key] = h
				}
				if h.last != st.handshakeNano {
					h.last = st.handshakeNano
					h.count++
				}
			}
		}
		if h := e.hs[p.Key]; h != nil {
			ps.Handshakes = h.count
		}
		ps.Reconnects = e.conns.count[p.ID]
		s.Peers = append(s.Peers, ps)
		if p.Kind == kindEntry && p.Candidate != "" {
			s.Entry = &EntryStatus{Candidate: p.Candidate, Relay: p.ID, Transport: ps.Transport, Since: e.entry.since.UnixMilli(), Switches: e.entry.switches}
		}
	}
	if e.cfg.isRelay() {
		s.Owners = map[string]OwnerStatus{}
		for _, n := range e.cfg.Nodes {
			if n.Role != roleMember {
				continue
			}
			if r, ok := e.attach.Owner(n.ID); ok {
				s.Owners[n.ID] = OwnerStatus{Relay: r}
			}
		}
	}
	if e.health != nil {
		hs := e.health.Snapshot()
		s.Error = hs.Error
		if hs.PropagationPending > 0 && s.Error == "" {
			s.Error = fmt.Sprintf("ownership propagation pending: %d acknowledgements", hs.PropagationPending)
		}
		paths := e.bind.(*streamBind).Paths()
		for i := range s.Peers {
			ps := &s.Peers[i]
			q := hs.Peers[ps.ID]
			if d, ok := hs.Directs[ps.ID]; ok && (ps.Kind == kindDirect || ps.Kind == kindRelay) {
				ps.Direct = &DirectStatus{State: d.State, Via: d.Via, Since: d.Since.UnixMilli(), Attempts: d.Attempts, LastError: d.LastError}
			}
			ps.RTTMs = float64(q.RTT) / float64(time.Millisecond)
			ps.TimeoutRate = q.TimeoutRate
			ps.Protocol = hs.Protocols[ps.ID]
			if r, ok := hs.LinkRedials[ps.ID]; ok {
				ps.Redial = redialStatus(&r)
			}
			ps.Endpoint = ""
			ps.Transport = ""
			ps.LocalAddr = ""
			ps.RemoteAddr = ""
			for _, p := range paths {
				if p.PeerID == ps.ID && p.Active {
					ps.Transport = p.Transport
					ps.Endpoint = stripScheme(p.Endpoint)
					ps.RemoteAddr = p.RemoteAddr
					if _, err := netip.ParseAddrPort(ps.RemoteAddr); err != nil {
						ps.RemoteAddr = ""
					}
					ps.LocalAddr = localAddrOf(p.LocalAddr, ps.RemoteAddr)
					break
				}
			}
			if ps.Kind == kindRelay && !ps.Active {
				for _, p := range paths {
					if p.PeerID == ps.ID && !p.LastAuthenticated.IsZero() && time.Since(p.LastAuthenticated) < relaySessionWindow {
						ps.Active = true
					}
				}
			}
		}
		if s.Entry == nil && e.cfg.Entry != nil && len(e.cfg.Entry.Candidates) > 0 {
			// No entry is up; the candidates still say why.
			s.Entry = &EntryStatus{}
		}
		if s.Entry != nil {
			if s.Entry.Candidate != "" {
				s.Entry.Since = hs.Since.UnixMilli()
				s.Entry.Switches = hs.Switches
			}
			for _, ps := range s.Peers {
				if ps.ID == s.Entry.Relay {
					s.Entry.Transport = ps.Transport
				}
			}
			s.Entry.Candidates = candidateHealth(e.cfg, hs)
		}
		s.Reachability = reachabilityStatus(hs.Reach)
		sec := hs.Security
		s.Security = &SecurityStatus{Signing: SigningStatus{Verified: sec.Verified, Rejected: sec.Rejected, ByHop: sec.RejectedBy}, Claims: ClaimsStatus{Expired: sec.ClaimsExpired}}
		if e.cfg.isRelay() {
			s.Owners = map[string]OwnerStatus{}
			for id, o := range hs.Owners {
				s.Owners[id] = OwnerStatus{Relay: o.Relay, Seq: o.Seq, LeaseMs: o.LeaseMs}
			}
		}
	}
	s.Ready = handshaken && e.host.ready()
	if c := e.cfg.Entry; c != nil && len(c.Candidates) > 0 {
		s.Ready = s.Ready && s.Entry != nil && s.Entry.Transport != ""
	}
	c := e.tun.stats
	s.Counters = CounterStatus{Delivered: c.Delivered.Load(), Forwarded: c.Forwarded.Load(), DroppedACL: c.DroppedACL.Load(),
		DroppedNotRelay: c.DroppedNotRelay.Load(), DroppedTTL: c.DroppedTTL.Load(), DroppedNoRoute: c.DroppedNoRoute.Load(),
		DroppedQueue: c.DroppedQueue.Load(), DroppedBad: c.DroppedBad.Load(), DroppedPlain: c.DroppedPlain.Load()}
	if e.e2e != nil {
		s.E2E = e.e2e.status()
	}
	s.Resources.QueueBytes = e.sched.queuedBytes()
	s.Resources.Conns = len(local)
	s.SkippedRoutes = []string{}
	for _, r := range e.skipped {
		s.SkippedRoutes = append(s.SkippedRoutes, r.String())
	}
	s.GatewayRoutes = []string{}
	for _, r := range e.hostCfg.Routes {
		if !e.cfg.Network.Prefix.Overlaps(r) {
			s.GatewayRoutes = append(s.GatewayRoutes, r.String())
		}
	}
	if r := e.router(); r != nil && e.cfg.isRelay() {
		s.Routing = routingStatus(r)
		// An idle link has no data-plane handshake or packet to show for
		// itself; the router's probes over the control plane do.
		for i := range s.Peers {
			ps := &s.Peers[i]
			if ps.Kind != kindRelay {
				continue
			}
			if j := slices.IndexFunc(s.Routing.Links, func(l RouteLink) bool { return l.Peer == ps.ID }); j >= 0 {
				ps.Active = s.Routing.Links[j].State == "up"
			}
		}
	}
	return s
}

func candidateHealth(c *Config, hs health.Snapshot) []CandidateHealth {
	out := []CandidateHealth{}
	for _, cand := range c.Entry.Candidates {
		i := slices.IndexFunc(hs.Candidates, func(x health.CandidateStatus) bool { return x.ID == cand.ID })
		if i < 0 {
			continue // not used: a fixed entry's later candidates
		}
		x := hs.Candidates[i]
		h := CandidateHealth{ID: cand.ID, Relay: cand.Relay, Transport: transportOf(cand.Endpoint), State: x.State, LastError: x.LastError, Via: x.Via}
		if cand.Punch {
			h.Transport = "udp"
		}
		if h.State == "" {
			h.State = "untested"
		}
		if !x.Since.IsZero() {
			h.Since = x.Since.UnixMilli()
		}
		if !x.LastOK.IsZero() {
			h.LastOKAt = x.LastOK.UnixMilli()
		}
		v := x.Measure
		h.Probes, h.Timeouts = v.Probes, v.Timeouts
		if !v.CheckedAt.IsZero() {
			h.CheckedAt = v.CheckedAt.UnixMilli()
		}
		if v.Recent > 0 {
			h.LossRate = &v.Loss
		}
		if v.RTT > 0 {
			ms := float64(v.RTT) / float64(time.Millisecond)
			h.RTTMs = &ms
		}
		h.Redial = redialStatus(x.Redial)
		out = append(out, h)
	}
	return out
}

func reachabilityStatus(r health.ReachStatus) *ReachabilityStatus {
	out := &ReachabilityStatus{Verdict: r.Verdict, Consistent: r.Consistent, Mapping: r.Mapping, Filtering: r.Filtering, Observers: []ObserverStatus{}}
	if !r.CheckedAt.IsZero() {
		out.CheckedAt = r.CheckedAt.UnixMilli()
	}
	if r.PublicAddr.IsValid() {
		out.PublicAddr = r.PublicAddr.String()
	}
	for _, o := range r.Observers {
		ob := ObserverStatus{ID: o.ID, Kind: o.Kind, OK: o.OK}
		if o.OK {
			ob.Addr = o.Addr.String()
		}
		out.Observers = append(out.Observers, ob)
	}
	return out
}

func fillProcess(s *Status, started time.Time, events *eventLog) {
	var m runtime.MemStats
	runtime.ReadMemStats(&m)
	s.Schema, s.PID, s.Version, s.StartedAt = statusSchema, os.Getpid(), Version, started.UnixMilli()
	s.ConfigSchemas = ConfigSchemas()
	s.Protocols = [2]int{protocol.Min, protocol.Max}
	s.Resources.HeapBytes = m.HeapInuse
	s.Resources.RSSBytes = residentBytes()
	s.Resources.Goroutines = runtime.NumGoroutine()
	s.Events = events.snapshot()
	if s.Peers == nil {
		s.Peers = []PeerStatus{}
	}
	if s.SkippedRoutes == nil {
		s.SkippedRoutes = []string{}
	}
	if s.GatewayRoutes == nil {
		s.GatewayRoutes = []string{}
	}
	s.HostFirewall = hostFirewall()
	s.HostFirewallIssues = hostFirewallIssues()
}

// FirewallIssue is something the host firewall does not let through, or may
// not: Code firewalld_zone (Detail the zone the user put the mesh device in),
// firewalld_failed, nftables_failed, docker_failed (Detail the error),
// iptables_legacy (iptables uses nf_tables but legacy tables hold rules) or
// iptables_nft (iptables is legacy but iptables-nft tables hold rules), not
// handled either way.
type FirewallIssue struct {
	Code   string `json:"code"`
	Detail string `json:"detail,omitempty"`
}

// cpuSampler gives the process's CPU use between two calls.
type cpuSampler struct {
	wall time.Time
	cpu  time.Duration
}

// percent is of one core since the last call, 0 the first time or when
// the CPU time is unknown.
func (c *cpuSampler) percent(now time.Time, used time.Duration, ok bool) float64 {
	last, lastCPU := c.wall, c.cpu
	if !ok {
		c.wall = time.Time{}
		return 0
	}
	c.wall, c.cpu = now, used
	if last.IsZero() || !now.After(last) || used < lastCPU {
		return 0
	}
	return math.Round(float64(used-lastCPU)/float64(now.Sub(last))*1000) / 10
}

// cachedCheck runs check in the background at most once per every and
// returns the last result meanwhile, "" before the first.
type cachedCheck struct {
	every   time.Duration
	check   func() string
	mu      sync.Mutex
	value   string
	last    time.Time
	running bool
}

func (c *cachedCheck) get() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.running && (c.last.IsZero() || time.Since(c.last) >= c.every) {
		c.running, c.last = true, time.Now()
		go func() {
			v := c.check()
			c.mu.Lock()
			c.value, c.running = v, false
			c.mu.Unlock()
		}()
	}
	return c.value
}

// firewallState reads socketfilterfw --getglobalstate: "Firewall is
// enabled. (State = 1)", 2 when it blocks all incoming connections.
func firewallState(out string) string {
	if _, v, ok := strings.Cut(out, "State = "); ok {
		if n, err := strconv.Atoi(strings.TrimRight(strings.Fields(v + " ")[0], ")")); err == nil {
			if n > 0 {
				return "on"
			}
			return ""
		}
	}
	if strings.Contains(strings.ToLower(out), "enabled") {
		return "on"
	}
	return ""
}

// goMappedBytes is the memory the Go runtime has from the OS and has not
// given back.
func goMappedBytes() uint64 {
	m := []metrics.Sample{{Name: "/memory/classes/total:bytes"}, {Name: "/memory/classes/heap/released:bytes"}}
	metrics.Read(m)
	if m[0].Value.Kind() != metrics.KindUint64 || m[1].Value.Kind() != metrics.KindUint64 {
		return 0
	}
	total, released := m[0].Value.Uint64(), m[1].Value.Uint64()
	if released > total {
		return 0
	}
	return total - released
}

// writeAtomic replaces path so a reader never sees half a file.
func writeAtomic(path string, v any) error {
	b, err := json.MarshalIndent(v, "", " ")
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err = os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	if err = os.Rename(tmp, path); err != nil {
		os.Remove(tmp)
	}
	return err
}

// meshState is state.json. Other keys, written by the health package, are
// kept as they are.
type meshState struct {
	Endpoints map[string]string `json:"endpoints"`
}

func loadState(dir string) meshState {
	var s meshState
	if dir == "" {
		return s
	}
	if raw, err := os.ReadFile(filepath.Join(dir, "state.json")); err == nil {
		json.Unmarshal(raw, &s)
	}
	return s
}

func saveEndpoints(dir string, endpoints map[string]string) error {
	return health.UpdateState(filepath.Join(dir, "state.json"), "endpoints", endpoints)
}
