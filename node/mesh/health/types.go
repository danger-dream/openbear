package health

import (
	"context"
	"crypto/ed25519"
	"net/netip"
	"time"

	"openbear/node/mesh/wire"
)

type Node struct {
	ID, Role, PublicKey, ControlKey string
	Address                         netip.Addr
	Relays                          []string
	// SignKey verifies what the node signs (signing.go).
	SignKey ed25519.PublicKey
}

type Candidate struct {
	ID, Peer, Endpoint string
	Pins               []string
	// Punch is an entry through a relay behind NAT, started only once a
	// punched path to it authenticated; Endpoint is one more address to try.
	Punch bool
}
type Link struct {
	Peer, Endpoint string
	Dial           bool
	Pins           []string
	// Punch is a UDP link both ends open through their NATs, between two
	// members or two relays: Endpoint, if any, is one more address to try.
	Punch bool
}

type Config struct {
	Network, Self, Role string
	Revision            int64
	Address             netip.Addr
	MTU                 int
	Nodes               []Node
	Candidates          []Candidate
	Links               []Link
	Mode                string
	AutoReturn          bool
	Parameters          Parameters
	// For punched links: the mesh prefix, never a candidate address, this
	// node's UDP listen port, and the relays' UDP addresses to reflect from.
	Prefix     netip.Prefix
	Port       int
	Reflectors []string
	NextHops   map[string]string
	// STUN servers asked besides the reflectors, host:port.
	STUN []string
	// Protocol is the mesh protocol range this node speaks (package wire);
	// zero is this build's.
	Protocol wire.Range
	// SignKey signs what this node says on its own behalf (signing.go).
	SignKey ed25519.PrivateKey
}

type Path struct {
	ID         string
	Generation uint64
}

type Received struct {
	Peer    string
	Path    Path
	At      time.Time
	Payload []byte
}

type WriteResult struct {
	ID                 uint64
	Started, Completed time.Time
	Err                error
}
type PathEvent struct {
	Path             Path
	Peer             string
	Closed, Security bool
	Error            string
}

type Backend interface {
	Prepare(context.Context, Candidate) (Path, error)
	Send(peer string, path Path, id uint64, payload []byte) error
	Commit(peer string, path Path) error
	Deactivate(peer string, path Path) error
	Release(path Path) error
	Remove(peer string) error
	Handshake(peer string, path Path) error
	DataReady(peer string, path Path) bool
	Warm(peer string, path Path)
	// Punch sends a business handshake to every address until the deadline;
	// with the deadline of an earlier call it adds the addresses to that one.
	Punch(peer string, addrs []netip.AddrPort, until time.Time) error
	// Prime sends each address a packet with the given IP TTL.
	Prime(addrs []netip.AddrPort, ttl int) error
	// Punched lists the peer's UDP paths that authenticated business
	// traffic, the first to authenticate first.
	Punched(peer string) []PunchedPath
	// Remote is the address at the other end of a path.
	Remote(path Path) (netip.AddrPort, bool)
	Reflect(ctx context.Context, server netip.AddrPort, id [8]byte) (netip.AddrPort, error)
	STUN(ctx context.Context, server netip.AddrPort) (netip.AddrPort, error)
	// UDPPort is the local port reflector and STUN requests go from.
	UDPPort() int
	// For reachability tests; see the transport package.
	ReflectSeen(id [8]byte) (netip.AddrPort, bool)
	RecentlyFrom(ip netip.Addr, within time.Duration) bool
	SendTest(target netip.AddrPort, id [8]byte, sender byte) error
	WatchTests(id [8]byte) bool
	TestsSeen(id [8]byte) byte
	StopTests(id [8]byte)
}

type PunchedPath struct {
	Path   Path
	Remote netip.AddrPort
	Active bool
}

type Hooks struct {
	Event  func(kind, detail string)
	Routes func() error
}

type CandidateStatus struct {
	ID, Peer                     string
	Path                         Path
	Ready, RouteReady, Preparing bool
	Probes                       ProbeStats
	// State is active, ready, failing, punching or untested; LastError one
	// of the candErr phrases.
	State, LastError string
	Since, LastOK    time.Time
	// Via is how a punched candidate's endpoint was found: lan, public or
	// configured; empty for others and until it is.
	Via string
	// Measure is what its probes measured (meter.go).
	Measure Measurement
	// Redial is nil for a candidate this node does not redial.
	Redial *RedialStatus
	// Suspect: end-to-end probes through it failed while it answered its
	// own probes, as a relay dropping business traffic does.
	Suspect bool
}

// RedialStatus is what status shows of a redialable path (redial.go).
type RedialStatus struct {
	// Baseline is zero until enough periods are judged.
	Baseline time.Duration
	// Level is how many redials since the path was last healthy for
	// GoodPeriods: the next waits Backoff[Level-1], or Backoff[0].
	Level, Redials int
	// Degraded is the bad periods in a row up to now.
	Degraded int
	// Last is the last redial, Next the earliest the next may be; both
	// zero before one.
	Last, Next time.Time
}

type Snapshot struct {
	Candidate          string
	Since              time.Time
	Switches           int
	Candidates         []CandidateStatus
	Peers              map[string]ProbeStats
	Owners             map[string]OwnerStatus
	Error              string
	PropagationPending int
	Directs            map[string]DirectStatus
	Reach              ReachStatus
	// Protocols is the mesh protocol control messages to each peer go out
	// in, for the peers whose range is known.
	Protocols map[string]int
	// LinkRedials is what status shows of the dialed TCP and TLS links'
	// redials, by peer.
	LinkRedials map[string]RedialStatus
	Security    SecurityStatus
}

// SecurityStatus counts the signatures of what nodes say on another's
// behalf this node checked: those that held, those that failed and these
// by the peer they came from. ClaimsExpired is the stored claims dropped
// since start for want of valid signatures.
type SecurityStatus struct {
	Verified, Rejected uint64
	RejectedBy         map[string]uint64
	ClaimsExpired      int
}

type ReachStatus struct {
	CheckedAt                   time.Time
	Verdict, Mapping, Filtering string
	PublicAddr                  netip.AddrPort
	Consistent                  bool
	Observers                   []Observer
}

type Observer struct {
	ID, Kind string
	Addr     netip.AddrPort
	OK       bool
}

type DirectStatus struct {
	State, Via, LastError string
	Since                 time.Time
	Attempts              int
}
