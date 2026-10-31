package health

import (
	"context"
	"crypto/ed25519"
	"encoding/json"
	"errors"
	"fmt"
	"math/rand/v2"
	"net/netip"
	"openbear/node/mesh/clock"
	"openbear/node/mesh/transport"
	"reflect"
	"slices"
	"sync"
	"syscall"
	"time"
)

// Errors status.json shows the user. Each is a stable phrase the controller
// and the console can match; the Go error behind it goes to the events.
const (
	errNoEntry        = "no ready entry"
	errRoutes         = "route update failed"
	errEntryState     = "entry state not saved"
	errEntryCertified = "entry certificate rejected"
)

// failReason is the short reason a path.failed event gives for err.
func failReason(err error) string {
	switch {
	case errors.Is(err, context.DeadlineExceeded):
		return "connect timeout"
	case errors.Is(err, transport.ErrSecurity):
		return "certificate rejected"
	case errors.Is(err, transport.ErrBackpressure):
		return "too many paths"
	case errors.Is(err, syscall.ECONNREFUSED):
		return "connection refused"
	}
	return "connect failed"
}

type candidateState struct {
	config                                 Candidate
	path                                   Path
	token                                  string
	track                                  *tracker
	pending                                map[uint64]string
	echoes                                 map[uint64]string // the answering side's echo of a pending probe's pong
	early                                  map[uint64]time.Time
	preparing                              bool
	ready                                  bool
	routeReady                             bool
	security                               bool
	since, next, retry, lastKick, lastWarm time.Time
	backoff                                time.Duration
	// suspect: until then the relay is taken to drop business traffic
	// while it answers probes (entrycheck.go).
	suspect time.Time
	// What status shows of an entry candidate.
	lastErr, state    string
	lastOK, stateFrom time.Time
	tried             bool
	meter             meter
	// trend judges a redialable path's probes; reprepare: the other end
	// asked for the path to be prepared again after a redial, while the
	// routes stay on it (redial.go).
	trend     *trend
	reprepare bool
	// reported is the outcome the last path.ready or path.failed told:
	// reportedReady, or the failure. Only a different one is an event, so
	// a cold candidate's check every ColdRetry, or a retry failing as the
	// one before, adds nothing to the event log.
	reported string
}

const reportedReady = "\x00ready"

// pathReady and pathFailed emit path.ready and path.failed for p when the
// outcome differs from the one last reported; pathFailed says whether it
// did.
func (m *Manager) pathReady(p *candidateState) {
	if p.reported != reportedReady {
		p.reported = reportedReady
		m.emit("path.ready", p.config.ID)
	}
}
func (m *Manager) pathFailed(p *candidateState, reason string) bool {
	if p.reported == reason {
		return false
	}
	p.reported = reason
	m.emit("path.failed", p.config.ID+": "+reason)
	return true
}

// Phrases status.json gives for an entry candidate's last failure.
const (
	candTimeout      = "timeout"
	candRefused      = "refused"
	candPin          = "tls pin mismatch"
	candNoHandshake  = "no handshake"
	candUnreachable  = "unreachable"
	candPunchTimeout = "punch timeout"
)

func candErr(err error) string {
	switch {
	case errors.Is(err, context.DeadlineExceeded):
		return candTimeout
	case errors.Is(err, transport.ErrSecurity):
		return candPin
	case errors.Is(err, syscall.ECONNREFUSED):
		return candRefused
	}
	return candUnreachable
}

type inbound struct {
	peer       string
	path       Path
	token      string
	generation uint64
	challenges map[string]time.Time
	// echoes: what each answered ping's pong asked the prober's "seen" to
	// repeat.
	echoes   map[string]string
	lastSeen time.Time
	prepared bool
	sequence uint64
}
type proposal struct {
	candidate string
	sequence  uint64
	nonce     string
	next      time.Time
	reason    string
	path      Path
	token     string
	expires   time.Time
	rescuing  bool
}

func newProposal(p *candidateState, sequence uint64, reason string, rescuing bool, now time.Time) *proposal {
	return &proposal{candidate: p.config.ID, sequence: sequence, nonce: nonce(), reason: reason,
		path: p.path, token: p.token, expires: now.Add(10 * time.Second), rescuing: rescuing}
}

type prepared struct {
	candidate string
	path      Path
	err       error
	revision  int64
}
type configRequest struct {
	cfg   Config
	reply chan error
}
type outboundNotice struct {
	notice        Notice
	peer          string
	next, expires time.Time
	since         time.Time // first send of an unanswered claim; kept across refreshes
}

// noticeGrace keeps the round trip of every routine refresh out of the status:
// only claims still unanswered after a few retries count as pending.
const noticeGrace = 3 * time.Second

// linkRetryLimit caps how long a lost relay or direct link waits between
// attempts; entry candidates back off up to five minutes.
const linkRetryLimit = 30 * time.Second

type Manager struct {
	mu          sync.RWMutex
	cfg         Config
	backend     Backend
	hooks       Hooks
	store       *store
	owners      *owners
	candidates  map[string]*candidateState
	links       map[string]*candidateState
	inbound     map[Path]*inbound
	active      string
	since       time.Time
	switches    int
	switchTimes []time.Time
	proposal    *proposal
	rescuing    bool
	nextID      uint64
	coldIndex   int
	nextCold    time.Time
	notices     map[string]outboundNotice
	lastRefresh map[string]time.Time
	changes     chan struct{}
	input       chan any
	inbox       *inbox // control messages, queued by peer (inbox.go)
	flushFailed bool   // the last write of staged claims failed
	ctx         context.Context
	cancel      context.CancelFunc
	done        chan struct{}
	error       string

	directs     map[string]*directState
	session     string
	epochs      map[string]uint64 // the newest session epoch of each node's signals (direct.go)
	offerGen    uint64
	local       []string // this member's offered addresses
	reflected   netip.AddrPort
	alsoSeen    []netip.AddrPort // what other reflectors saw, if it differs
	reflectNext time.Time
	reflecting  bool
	discovered  bool
	// observed is what each entry candidate saw this node's pings come
	// from (entrycheck.go).
	observed   map[string]observation
	mismatchAt time.Time
	offerRates map[string]offerRate
	reach      reachState
	started    time.Time

	relay relayRouting // routing.go

	// signing.go: the signing keys of the config's nodes and this one, how
	// many signatures held and failed, the failed by the peer they came
	// from, the stored claims dropped for theirs, and when each kind of
	// event was last emitted.
	signKeys      map[string]ed25519.PublicKey
	signVerified  uint64
	signRejected  uint64
	rejectedBy    map[string]uint64
	claimsExpired int
	limited       map[string]limitedEvent

	protocols map[string]peerProtocol // version.go

	// redial.go
	redial        RedialParams
	baselines     map[string]savedTrend
	baselineSaved time.Time
}

func New(dir string, c Config, b Backend, h Hooks) (*Manager, error) {
	s, err := openStore(dir, c.Network, c.Self)
	if err != nil {
		return nil, err
	}
	epoch, err := nextEpoch(s.path, time.Now())
	if err != nil {
		return nil, err
	}
	ctx, cancel := context.WithCancel(context.Background())
	m := &Manager{protocols: map[string]peerProtocol{}, backend: b, hooks: h, store: s, owners: newOwners(s, c.Parameters.Lease), candidates: map[string]*candidateState{}, links: map[string]*candidateState{}, inbound: map[Path]*inbound{}, notices: map[string]outboundNotice{}, lastRefresh: map[string]time.Time{}, changes: make(chan struct{}, 1), input: make(chan any, 256), inbox: newInbox(), ctx: ctx, cancel: cancel, done: make(chan struct{}),
		directs: map[string]*directState{}, session: fmt.Sprintf("%d-%s", epoch, nonce()), epochs: map[string]uint64{}, offerRates: map[string]offerRate{}, reach: newReach(), started: time.Now(),
		redial: currentRedialParams(), baselines: loadBaselines(s.path), baselineSaved: time.Now()}
	if err = m.configure(c); err != nil {
		cancel()
		return nil, err
	}
	go m.run()
	return m, nil
}

func (m *Manager) Close()                   { m.cancel(); <-m.done }
func (m *Manager) Changes() <-chan struct{} { return m.changes }
func (m *Manager) Owner(member string) (string, bool) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.owners.owner(member, time.Now())
}
func (m *Manager) ActiveEntry() (string, bool) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.active, m.active != ""
}
func (m *Manager) Snapshot() Snapshot {
	m.mu.Lock()
	defer m.mu.Unlock()
	s := Snapshot{Candidate: m.active, Since: m.since, Switches: m.switches, Owners: m.owners.snapshot(time.Now()), Peers: map[string]ProbeStats{}, Error: m.error, PropagationPending: m.pendingNotices(time.Now())}
	for _, p := range m.candidates {
		via := ""
		if d := m.directs[p.config.Peer]; d != nil && d.kind == punchEntry && p.config.Punch && p.config.Endpoint != "" {
			via = d.via
		}
		s.Candidates = append(s.Candidates, CandidateStatus{p.config.ID, p.config.Peer, p.path, p.ready, p.routeReady, p.preparing, p.track.stats, p.state, p.lastErr, p.stateFrom, p.lastOK, via, p.meter.measurement(), m.redialStatus(p, time.Now()), time.Now().Before(p.suspect)})
		if p.config.ID == m.active {
			s.Peers[p.config.Peer] = p.track.stats
		}
	}
	for id, p := range m.links {
		s.Peers[id] = p.track.stats
		if r := m.redialStatus(p, time.Now()); r != nil {
			if s.LinkRedials == nil {
				s.LinkRedials = map[string]RedialStatus{}
			}
			s.LinkRedials[id] = *r
		}
	}
	for id, d := range m.directs {
		if d.link != nil {
			s.Peers[id] = d.link.track.stats
		}
	}
	s.Protocols = m.protocolSnapshot(time.Now())
	s.Directs = m.directSnapshot()
	s.Reach = m.reach.status
	s.Reach.Observers = slices.Clone(s.Reach.Observers)
	s.Security = m.securityStatus()
	return s
}
func (m *Manager) Receive(r Received) bool {
	if m.ctx.Err() != nil {
		return false
	}
	return m.inbox.push(r)
}
func (m *Manager) Written(w WriteResult) bool   { return m.enqueue(w) }
func (m *Manager) PathChanged(e PathEvent) bool { return m.enqueue(e) }
func (m *Manager) enqueue(v any) bool {
	select {
	case m.input <- v:
		return true
	case <-m.ctx.Done():
		return false
	default:
		return false
	}
}
func (m *Manager) ValidateConfig(c Config) error {
	m.mu.RLock()
	defer m.mu.RUnlock()
	if c.Revision < m.cfg.Revision {
		return errors.New("health revision regressed")
	}
	if c.Revision == m.cfg.Revision && !reflect.DeepEqual(c, m.cfg) {
		return errors.New("health change requires a newer revision")
	}
	return nil
}

func (m *Manager) Configure(c Config) error {
	q := configRequest{c, make(chan error, 1)}
	select {
	case m.input <- q:
	case <-m.done:
		return errors.New("health closed")
	}
	select {
	case err := <-q.reply:
		return err
	case <-m.done:
		return errors.New("health closed")
	}
}

func (m *Manager) configure(c Config) error {
	if err := c.Parameters.Validate(); err != nil {
		return err
	}
	if len(c.Candidates) > 8 {
		return errors.New("at most eight entry candidates are supported")
	}
	if len(c.SignKey) != ed25519.PrivateKeySize {
		return errors.New("no signing key")
	}
	keys := signKeys(c)
	for _, n := range c.Nodes {
		if keys[n.ID] == nil {
			return fmt.Errorf("node %s has no signing key", n.ID)
		}
	}
	allowed := map[string][]string{}
	relays := map[string]bool{}
	if c.Role == "relay" {
		relays[c.Self] = true
	}
	known := map[string]bool{}
	for _, n := range c.Nodes {
		known[n.ID] = true
		if n.Role == "relay" {
			relays[n.ID] = true
		} else {
			allowed[n.ID] = n.Relays
		}
	}
	dropped, err := m.owners.configure(c.Revision, allowed, relays, c.Network, keys)
	if err != nil {
		return err
	}
	if dropped > 0 {
		m.claimsExpired += dropped
		// Claims stored before claims were signed, or under a member's or
		// relay's key from before it was reinstalled: they count as expired,
		// as every claim does after a restart until its lease is renewed.
		m.emit("owner.claim_expired", fmt.Sprintf("%d stored claims without valid signatures dropped", dropped))
	}
	m.pruneEpochs(keys)
	m.signKeys = keys
	now := time.Now()
	// A punched candidate's endpoint is the path it punched, not config.
	same := func(a, b Candidate) bool {
		return a.ID == b.ID && a.Peer == b.Peer && a.Punch == b.Punch && (a.Punch || a.Endpoint == b.Endpoint) && slices.Equal(a.Pins, b.Pins)
	}
	keep := map[string]*candidateState{}
	for i, cand := range c.Candidates {
		if c.Mode == "fixed" && i > 0 {
			break
		}
		p := m.candidates[cand.ID]
		if p == nil || !same(p.config, cand) {
			old := p
			p = &candidateState{config: cand, track: newTracker(c.Parameters, now), pending: map[uint64]string{}, early: map[uint64]time.Time{}}
			if cand.Punch {
				p.config.Endpoint = ""
			}
			if old != nil {
				// The counts go on under the candidate's ID.
				p.meter = old.meter
				p.meter.restart()
				if old.config.Endpoint == cand.Endpoint {
					p.trend = old.trend
				}
			}
		}
		p.track.params = c.Parameters
		keep[cand.ID] = p
	}
	for id, p := range m.candidates {
		if keep[id] != p {
			if p.path.ID != "" {
				m.backend.Deactivate(p.config.Peer, p.path)
				m.backend.Release(p.path)
			}
			if m.active == id {
				m.active = ""
			}
		}
	}
	m.configureDirects(c, now)
	oldNodes := m.cfg.Nodes
	m.cfg = c
	m.candidates = keep
	m.owners.lease = c.Parameters.Lease
	for _, n := range oldNodes {
		if !known[n.ID] {
			m.backend.Remove(n.ID)
		}
	}
	for p, in := range m.inbound {
		if !known[in.peer] {
			delete(m.inbound, p)
		}
	}
	for _, l := range c.Links {
		if !l.Dial || l.Punch {
			continue
		}
		cand := Candidate{ID: "link:" + l.Peer, Peer: l.Peer, Endpoint: l.Endpoint, Pins: l.Pins}
		if p := m.links[l.Peer]; p == nil || !same(p.config, cand) {
			var trend *trend
			if p != nil && p.path.ID != "" {
				m.backend.Deactivate(p.config.Peer, p.path)
				m.backend.Release(p.path)
			}
			if p != nil && p.config.Endpoint == cand.Endpoint {
				trend = p.trend
			}
			m.links[l.Peer] = &candidateState{config: cand, track: newTracker(c.Parameters, now), pending: map[uint64]string{}, early: map[uint64]time.Time{}, trend: trend}
		}
	}
	for id, p := range m.links {
		if !slices.ContainsFunc(c.Links, func(l Link) bool { return l.Peer == id && l.Dial && !l.Punch }) {
			m.backend.Deactivate(p.config.Peer, p.path)
			m.backend.Release(p.path)
			delete(m.links, id)
		}
	}
	for id := range m.lastRefresh {
		if !known[id] {
			delete(m.lastRefresh, id)
		}
	}
	m.proposal = nil
	m.nextCold = now.Add(c.Parameters.ColdRetry)
	m.notices = map[string]outboundNotice{}
	m.error = ""
	return nil
}

func (m *Manager) run() {
	defer close(m.done)
	defer func() {
		m.mu.Lock()
		m.flushState(time.Now(), true)
		m.mu.Unlock()
	}()
	tick := time.NewTicker(50 * time.Millisecond)
	defer tick.Stop()
	for {
		select {
		case <-m.ctx.Done():
			return
		case <-tick.C:
			// The tick's own time is time.Now's: what it stamps (a trial
			// ping's sending) is measured against answers stamped with
			// clock.Now.
			now := clock.Now()
			m.mu.Lock()
			m.tick(now)
			m.flushState(now, false)
			m.mu.Unlock()
		case <-m.inbox.ready:
			// A batch at a time, so that a flood does not hold off the tick.
			for range inboxBatch {
				r, ok := m.inbox.pop()
				if !ok {
					break
				}
				m.mu.Lock()
				m.receive(r)
				m.mu.Unlock()
			}
			m.inbox.again()
		case v := <-m.input:
			m.mu.Lock()
			switch v := v.(type) {
			case Received:
				m.receive(v)
			case WriteResult:
				m.written(v)
			case PathEvent:
				m.pathEvent(v)
			case prepared:
				m.prepared(v)
			case configRequest:
				v.reply <- m.configure(v.cfg)
			case reflected:
				m.reflectedAddr(v, time.Now())
			case reachResult:
				m.reachDone(v, time.Now())
			case redialDone:
				m.redialed(v, time.Now())
			case entrySuspect:
				m.entrySuspect(v, time.Now())
			}
			m.mu.Unlock()
		}
	}
}
func (m *Manager) emit(kind, detail string) {
	if m.hooks.Event != nil {
		m.hooks.Event(kind, detail)
	}
}
func (m *Manager) changed() {
	select {
	case m.changes <- struct{}{}:
	default:
	}
}
func (m *Manager) routes() error {
	m.changed()
	var err error
	if m.hooks.Routes != nil {
		m.mu.Unlock()
		err = m.hooks.Routes()
		m.mu.Lock()
	}
	if err != nil {
		m.error = errRoutes
		m.emit("route.error", err.Error())
	} else if m.error == errRoutes {
		m.error = ""
	}
	return err
}

// stateFlush is how often claims staged on a relay are written: one write
// a second at most, however many notices come.
const stateFlush = time.Second

// flushState writes what the store staged, when due or forced.
func (m *Manager) flushState(now time.Time, force bool) {
	if !m.store.dirty || (!force && now.Sub(m.store.written) < stateFlush) {
		return
	}
	err := m.store.flush()
	if err != nil && !m.flushFailed {
		m.emit("state.error", err.Error())
	}
	m.flushFailed = err != nil
}

func jitter(d time.Duration, f float64) time.Duration {
	return time.Duration(float64(d) * (1 - f + 2*f*rand.Float64()))
}

// startable is false for a punched candidate until it has a path.
func startable(p *candidateState) bool { return !p.config.Punch || p.config.Endpoint != "" }

func (m *Manager) start(p *candidateState, now time.Time) {
	if p.preparing || p.path.ID != "" || now.Before(p.retry) || p.security || !startable(p) {
		return
	}
	p.tried = true
	if m.candidates[p.config.ID] != nil {
		count := 0
		for _, q := range m.candidates {
			if q.preparing || q.path.ID != "" {
				count++
			}
		}
		if count >= 3 {
			return
		}
	}
	p.preparing = true
	p.since = now
	cand, revision := p.config, m.cfg.Revision
	go func() {
		ctx, cancel := context.WithTimeout(m.ctx, 10*time.Second)
		defer cancel()
		path, err := m.backend.Prepare(ctx, cand)
		select {
		case m.input <- prepared{cand.ID, path, err, revision}:
		case <-m.ctx.Done():
			if path.ID != "" {
				m.backend.Release(path)
			}
		}
	}()
}
func (m *Manager) find(id string) *candidateState {
	if p := m.candidates[id]; p != nil {
		return p
	}
	for _, p := range m.links {
		if p.config.ID == id {
			return p
		}
	}
	for _, d := range m.directs {
		if d.link != nil && d.link.config.ID == id {
			return d.link
		}
		if d.upcoming != nil && d.upcoming.config.ID == id {
			return d.upcoming
		}
	}
	return nil
}
func (m *Manager) prepared(v prepared) {
	p := m.find(v.candidate)
	if p == nil || v.revision != m.cfg.Revision {
		if p != nil {
			p.preparing = false
		}
		if v.path.ID != "" {
			m.backend.Release(v.path)
		}
		return
	}
	p.preparing = false
	if d := m.directOf(p); d != nil && v.err != nil {
		if d.upcoming == p {
			m.abortUpcoming(d, "prepare failed")
		} else {
			m.punchFailed(d, time.Now())
		}
		return
	}
	if v.err != nil {
		p.meter.unreachable(time.Now())
		p.lastErr = candErr(v.err)
		reported := m.pathFailed(p, failReason(v.err))
		if errors.Is(v.err, transport.ErrSecurity) {
			p.security = true
			if reported {
				m.emit("path.security", v.err.Error())
			}
		}
		m.failed(p, time.Now())
		if p.security {
			m.certificateRejected(p)
		}
		return
	}
	p.path = v.path
	p.token = nonce()
	p.track = newTracker(m.cfg.Parameters, time.Now())
	p.ready = false
	p.routeReady = false
	p.pending = map[uint64]string{}
	p.echoes = nil
	p.early = map[uint64]time.Time{}
	p.next = time.Now()
	m.backend.Handshake(p.config.Peer, p.path)
	p.lastKick = time.Now()
}
func (m *Manager) failed(p *candidateState, now time.Time) {
	if p.path.ID != "" && (p.config.ID != m.active || p.config.Punch) {
		m.backend.Deactivate(p.config.Peer, p.path)
		m.backend.Release(p.path)
		p.path = Path{}
	}
	if p.config.Punch && p.config.ID == m.active {
		m.active = ""
	}
	p.ready = false
	p.routeReady = false
	limit := 300 * time.Second
	if m.links[p.config.Peer] == p {
		// Traffic detours while a link is down; once the peer is back,
		// a long wait to try again only keeps it on the detour.
		limit = max(m.cfg.Parameters.ColdRetry, linkRetryLimit)
	}
	if p.backoff == 0 {
		p.backoff = m.cfg.Parameters.ColdRetry
	} else {
		p.backoff = min(limit, p.backoff*2)
	}
	p.retry = now.Add(min(limit, jitter(p.backoff, .2)))
	p.next = p.retry
	m.entryLost(p, now)
	m.noEntry()
}

// noEntry reports a member with entry candidates that has no entry and
// no candidate ready to become one. Other failures are only events: while
// an entry carries traffic, there is nothing for the user to do.
func (m *Manager) noEntry() {
	if m.active != "" || len(m.cfg.Candidates) == 0 || m.error == errNoEntry || m.error == errEntryCertified {
		return
	}
	for _, p := range m.candidates {
		if p.ready {
			return
		}
	}
	m.error = errNoEntry
	m.emit("entry.unavailable", m.error)
}

// certificateRejected is an error only for the entry in use, or the first
// one a member without an entry tries: another candidate is a path.failed.
func (m *Manager) certificateRejected(p *candidateState) {
	if m.candidates[p.config.ID] == p && (p.config.ID == m.active || m.active == "") {
		m.error = errEntryCertified
	}
}
func (m *Manager) send(peer string, path Path, v message, id uint64) error {
	v.Version = m.sendVersion(peer, time.Now())
	if v.Type != msgAdvert {
		// An advertisement's room goes to the advertisement; the probes on
		// every relay link, each second, declare the range.
		local := m.cfg.Protocol.Or()
		v.Min, v.Max = local.Min, local.Max
	}
	v.Network = m.cfg.Network
	v.Revision = m.cfg.Revision
	v.From = m.cfg.Self
	v.To = peer
	raw, err := json.Marshal(v)
	if err != nil {
		return err
	}
	if len(raw) > maxMessage {
		return errors.New("control message exceeds limit")
	}
	return m.backend.Send(peer, path, id, raw)
}
func (m *Manager) probe(p *candidateState, now time.Time) {
	m.nextID++
	id := m.nextID
	if !p.track.queue(id, now) {
		return
	}
	n := nonce()
	p.pending[id] = n
	d := m.directOf(p)
	m.routeSent(p, id, now)
	if err := m.send(p.config.Peer, p.path, message{Type: "ping", Nonce: n, Token: p.token, Generation: p.path.Generation, Probe: id, Direct: d != nil && d.local}, id); err != nil {
		p.track.written(id, now, now, err)
		delete(p.pending, id)
	}
	interval := m.cfg.Parameters.StandbyInterval
	// Relay links are probed every active interval for routing.
	if p.config.ID == m.active || !p.ready || d != nil || m.links[p.config.Peer] == p {
		interval = m.cfg.Parameters.ActiveInterval
	}
	p.next = now.Add(jitter(interval, .1))
}
func (m *Manager) allPaths() []*candidateState {
	out := make([]*candidateState, 0, len(m.candidates)+len(m.links))
	for _, p := range m.candidates {
		out = append(out, p)
	}
	for _, p := range m.links {
		out = append(out, p)
	}
	for _, d := range m.directs {
		if d.link != nil {
			out = append(out, d.link)
		}
		if d.upcoming != nil {
			out = append(out, d.upcoming)
		}
	}
	return out
}
func (m *Manager) written(w WriteResult) {
	for _, p := range m.allPaths() {
		if _, ok := p.pending[w.ID]; !ok {
			continue
		}
		p.track.written(w.ID, w.Started, w.Completed, w.Err)
		if at, ok := p.early[w.ID]; ok {
			delete(p.early, w.ID)
			m.success(p, w.ID, at)
		}
		return
	}
}
func maxTime(a, b time.Time) time.Time {
	if a.Before(b) {
		return b
	}
	return a
}
func (m *Manager) success(p *candidateState, id uint64, at time.Time) {
	answered := p.track.reply(id, at)
	m.expire(p, at) // a late answer
	if !answered {
		return
	}
	p.meter.answered(at, p.track.stats.RTT)
	m.observe(p, at, p.track.stats.RTT, false)
	m.send(p.config.Peer, p.path, message{Type: "seen", Nonce: p.pending[id], Token: p.token, Generation: p.path.Generation, Probe: id, Echo: p.echoes[id]}, 0)
	delete(p.pending, id)
	delete(p.echoes, id)
	p.lastOK = at
	m.routeReply(p, id, p.track.stats.RTT)
	if p.track.stats.Successes >= m.cfg.Parameters.ReadySuccesses {
		if p.config.ID == m.active && m.error == errNoEntry {
			m.error = ""
		}
		p.ready = true
		p.backoff = 0
	}
}

func (m *Manager) tick(now time.Time) {
	m.expireProtocols(now)
	for _, p := range m.links {
		m.start(p, now)
	}
	if len(m.cfg.Candidates) > 0 {
		if active := m.candidates[m.active]; active != nil {
			m.start(active, now)
		}
		if m.active == "" {
			// A punched first candidate needs another to signal through.
			for _, c := range m.cfg.Candidates {
				if p := m.candidates[c.ID]; p != nil && startable(p) {
					m.start(p, now)
					break
				}
			}
		}
		if m.cfg.Mode == "failover" {
			for _, c := range m.cfg.Candidates {
				p := m.candidates[c.ID]
				if p != nil && c.ID != m.active && !p.security && !now.Before(p.retry) && startable(p) {
					m.start(p, now)
					break
				}
			}
			if !now.Before(m.nextCold) {
				m.nextCold = now.Add(jitter(m.cfg.Parameters.ColdRetry, .2))
				for i := 0; i < len(m.cfg.Candidates); i++ {
					m.coldIndex = (m.coldIndex + 1) % len(m.cfg.Candidates)
					p := m.candidates[m.cfg.Candidates[m.coldIndex].ID]
					if p != nil && p.config.ID != m.active && p.path.ID == "" {
						m.start(p, now)
						break
					}
				}
			}
		}
	}
	for _, p := range m.allPaths() {
		if m.directOf(p) != nil {
			continue
		}
		more := m.trackTick(p, now)
		if p.path.ID == "" {
			continue
		}
		if more && now.Sub(p.lastKick) >= m.cfg.Parameters.HandshakeGrace {
			m.backend.Handshake(p.config.Peer, p.path)
			p.lastKick = now
		}
		if p.ready && (p.config.ID != m.active || p.config.Punch) && m.candidates[p.config.ID] != nil && p.track.stats.Failures >= m.cfg.Parameters.ConsecutiveFailures {
			p.lastErr = candTimeout
			m.pathFailed(p, "probe timeout")
			m.failed(p, now)
			continue
		}
		if (!p.ready || !p.routeReady) && now.Sub(p.since) > 10*time.Second && (p.config.ID != m.active || p.config.Punch) {
			reason := "probe timeout"
			p.lastErr = candTimeout
			if p.ready {
				reason = "relay not ready"
				p.lastErr = candNoHandshake
			}
			m.pathFailed(p, reason)
			m.failed(p, now)
			continue
		}
		if !now.Before(p.next) {
			m.probe(p, now)
		}
		if p.config.ID != m.active && m.candidates[p.config.ID] != nil && now.Sub(p.since) > 10*time.Second && p.config.ID != m.standby(now) && (m.proposal == nil || m.proposal.candidate != p.config.ID) {
			m.backend.Release(p.path)
			p.path = Path{}
			p.ready = false
			p.routeReady = false
			p.retry = now.Add(m.cfg.Parameters.ColdRetry)
			continue
		}
		if p.ready && (!p.routeReady || p.reprepare) {
			if now.Sub(p.lastWarm) >= time.Second {
				m.backend.Warm(p.config.Peer, p.path)
				p.lastWarm = now
			}
			m.send(p.config.Peer, p.path, message{Type: "prepare", Token: p.token, Generation: p.path.Generation}, 0)
		}
	}
	m.redialTick(now)
	m.tickDirects(now)
	m.reachTick(now)
	m.routingTick(now)
	m.choose(now)
	m.candidateStates(now)
	if q := m.proposal; q != nil && !now.Before(q.next) {
		p := m.candidates[q.candidate]
		if p != nil && p.path.ID != "" && p.routeReady {
			msig := claimInput(m.cfg.Network, m.cfg.Self, p.config.Peer, q.sequence).SignString(m.cfg.SignKey)
			m.send(p.config.Peer, p.path, message{Type: "commit", Token: p.token, Generation: p.path.Generation, Sequence: q.sequence, Nonce: q.nonce, MSig: msig}, 0)
		}
		q.next = now.Add(time.Second)
	}
	m.refresh(now)
	for key, n := range m.notices {
		if !now.Before(n.expires) {
			m.emit("owner.unconfirmed", fmt.Sprintf("%s member=%s seq=%d: lease expired without ACK", n.peer, n.notice.Member, n.notice.Seq))
			delete(m.notices, key)
			continue
		}
		if !now.Before(n.next) {
			if p, ok := m.peerPath(n.peer); ok {
				m.send(n.peer, p, message{Type: "owner", Notice: &n.notice}, 0)
			}
			n.next = now.Add(time.Second)
			m.notices[key] = n
		}
	}
	expired := false
	for id, until := range m.owners.expires {
		if !now.Before(until) {
			delete(m.owners.expires, id)
			for _, in := range m.inbound {
				if in.peer == id && in.sequence > 0 {
					m.backend.Deactivate(in.peer, in.path)
					in.sequence = 0
				}
			}
			expired = true
		}
	}
	if expired {
		m.routes()
	}
	for path, in := range m.inbound {
		for n, t := range in.challenges {
			if now.Sub(t) > 10*time.Second {
				delete(in.challenges, n)
				delete(in.echoes, n)
			}
		}
		if now.Sub(in.lastSeen) > 2*m.cfg.Parameters.Lease && len(in.challenges) == 0 {
			delete(m.inbound, path)
		}
	}
}

// trackTick expires p's probes and says whether that added failures.
func (m *Manager) trackTick(p *candidateState, now time.Time) bool {
	before := p.track.stats.Failures
	p.track.tick(now)
	m.expire(p, now)
	for id := range p.pending {
		if _, ok := p.track.pending[id]; !ok {
			if _, expired := p.track.expired[id]; !expired {
				delete(p.pending, id)
			}
		}
	}
	for id := range p.echoes {
		if _, ok := p.pending[id]; !ok {
			delete(p.echoes, id)
		}
	}
	return p.track.stats.Failures > before
}

// candidateStates follows what status shows of each entry candidate.
func (m *Manager) candidateStates(now time.Time) {
	for _, p := range m.candidates {
		state := "untested"
		d := m.directs[p.config.Peer]
		switch {
		case p.config.ID == m.active:
			state = "active"
		case p.ready && p.routeReady && p.track.stats.Failures == 0:
			state = "ready"
		case p.config.Punch && p.config.Endpoint == "" && d != nil && d.active:
			state = "punching"
		case p.lastErr != "":
			state = "failing"
		case p.config.Punch && p.config.Endpoint == "":
			state = "punching"
		}
		if state == "ready" || state == "active" {
			p.lastErr = ""
		}
		if state != p.state {
			p.state, p.stateFrom = state, now
		}
	}
}

func (m *Manager) standby(now time.Time) string {
	for _, c := range m.cfg.Candidates {
		p := m.candidates[c.ID]
		if p != nil && c.ID != m.active && !p.security && !now.Before(p.retry) && startable(p) {
			return c.ID
		}
	}
	return ""
}

func (m *Manager) choose(now time.Time) {
	current := m.candidates[m.active]
	dead := current == nil || !current.ready || current.track.stats.Failures >= m.cfg.Parameters.ConsecutiveFailures
	if q := m.proposal; q != nil {
		p := m.candidates[q.candidate]
		samePath := p != nil && p.path == q.path && p.token == q.token
		if samePath && !p.security && p.path.ID != "" && p.track.stats.Failures < m.cfg.Parameters.ConsecutiveFailures && now.Before(q.expires) && (!dead || q.rescuing) {
			return
		}
		m.proposal = nil
		m.emit("entry.aborted", fmt.Sprintf("%s seq=%d: path failed or commit expired", q.candidate, q.sequence))
		if samePath {
			m.failed(p, now)
		}
		current = m.candidates[m.active]
		dead = current == nil || !current.ready || current.track.stats.Failures >= m.cfg.Parameters.ConsecutiveFailures
	}
	if current == nil && len(m.cfg.Candidates) > 0 {
		if p := m.candidates[m.cfg.Candidates[0].ID]; p != nil && p.security {
			return
		}
	}
	suspect := current != nil && !dead && now.Before(current.suspect)
	dead = dead || suspect
	if dead && !m.rescuing {
		for _, p := range m.candidates {
			if p.config.ID != m.active && !p.security {
				p.retry = now
			}
		}
	}
	m.rescuing = dead
	if current != nil && current.security {
		return
	}
	for index, c := range m.cfg.Candidates {
		p := m.candidates[c.ID]
		if p == nil || c.ID == m.active || !p.ready || !p.routeReady || p.security || p.track.stats.Failures != 0 || now.Before(p.suspect) {
			continue
		}
		selectIt := dead
		if current != nil && !dead {
			n, rate := p.track.quality(now)
			selectIt = current.track.badWindows >= 2 && n >= 20 && rate <= .1 && now.Sub(m.since) >= m.cfg.Parameters.MinResidence
			currentIndex := slices.IndexFunc(m.cfg.Candidates, func(c Candidate) bool { return c.ID == m.active })
			selectIt = selectIt || (m.cfg.AutoReturn && index < currentIndex && p.track.stable(now) && now.Sub(m.since) >= m.cfg.Parameters.MinResidence)
		}
		if !selectIt {
			continue
		}
		seq, err := m.store.nextSequence()
		if err != nil {
			if m.error != errEntryState {
				m.emit("state.error", err.Error())
			}
			m.error = errEntryState
			return
		}
		reason := "initial"
		if current != nil {
			reason = "sustained degradation or stable preferred path"
			if suspect {
				reason = "end-to-end probes through the entry failed"
			} else if dead {
				reason = "active path failed"
			}
		}
		m.proposal = newProposal(p, seq, reason, dead, now)
		m.emit("entry.prepare", fmt.Sprintf("%s seq=%d: %s", c.ID, seq, reason))
		return
	}
	if dead && !suspect && current != nil && m.error != errNoEntry {
		m.error = errNoEntry
		m.emit("entry.unavailable", m.error)
	}
}

func (m *Manager) pathEvent(e PathEvent) {
	if e.Error != "" && !e.Security {
		m.emit("path.error", e.Peer+": "+e.Error)
	}
	for _, p := range m.allPaths() {
		if p.path != e.Path {
			continue
		}
		if e.Security {
			p.security = true
			if m.pathFailed(p, "certificate rejected") {
				m.emit("path.security", e.Error)
			}
			m.certificateRejected(p)
		}
		if e.Closed {
			p.reprepare = false
			if p.trend != nil {
				p.trend.swapped, p.trend.sawReclaim = time.Time{}, false
			}
			if !p.security {
				p.lastErr = candTimeout
			}
			p.track.stats.Failures = m.cfg.Parameters.ConsecutiveFailures
			p.ready = false
			p.routeReady = false
			p.path = Path{}
			p.retry = time.Now()
			if p.config.Punch {
				m.entryLost(p, p.retry)
				if p.config.ID == m.active {
					m.active = ""
				}
			}
			if p.config.ID == m.active {
				m.start(p, time.Now())
			}
		}
	}
	delete(m.inbound, e.Path)
}

func (m *Manager) receive(r Received) {
	// The revision is not compared: the controller sends a new revision only
	// to the nodes whose config changed, so peers routinely run different
	// ones. Authorization comes from this node's own config.
	v, err := decode(r.Payload)
	if err != nil || v.Network != m.cfg.Network || v.From != r.Peer || v.To != m.cfg.Self {
		return
	}
	now := r.At
	if now.IsZero() {
		now = time.Now()
	}
	// The peer is the control WireGuard key's owner: its range is learned
	// also from a message in a version this node does not speak, which is
	// then dropped; the answer to the next one will be in a common version.
	m.learnProtocol(r.Peer, v, now)
	if !m.cfg.Protocol.Or().Has(v.Version) {
		return
	}
	switch v.Type {
	case "ping":
		if len(v.Nonce) != 32 || len(v.Token) != 32 || v.Probe == 0 {
			return
		}
		in := m.inbound[r.Path]
		if v.Trial && in != nil && in.peer == r.Peer && in.token != v.Token {
			// A punch attempt measuring a path that arrives on this one, as
			// two of the peer's may through a NAT: answered, and this path's
			// state left to whatever probes it.
			v.Type, v.Direct, v.Reclaim, v.Echo, v.Observed = "pong", false, false, "", ""
			if d := m.directs[r.Peer]; d != nil {
				v.Direct = d.local
			}
			m.send(r.Peer, r.Path, v, 0)
			return
		}
		if in == nil || in.peer != r.Peer || in.token != v.Token || in.generation != v.Generation {
			count := 0
			var stale *inbound
			for _, x := range m.inbound {
				if x.peer == r.Peer {
					count++
					// No ping for 10 s: a path no longer probed, as one a punch
					// attempt measured.
					if len(x.challenges) == 0 && now.Sub(x.lastSeen) > 10*time.Second && (stale == nil || x.lastSeen.Before(stale.lastSeen)) {
						stale = x
					}
				}
			}
			if count >= 3 && in == nil {
				if stale == nil {
					return
				}
				delete(m.inbound, stale.path)
			}
			in = &inbound{peer: r.Peer, path: r.Path, token: v.Token, generation: v.Generation, challenges: map[string]time.Time{}}
			m.inbound[r.Path] = in
		}
		if len(in.challenges) >= 16 {
			return
		}
		in.challenges[v.Nonce] = now
		if in.echoes == nil {
			in.echoes = map[string]string{}
		}
		v.Echo = nonce()
		in.echoes[v.Nonce] = v.Echo
		v.Type, v.Observed = "pong", ""
		if a, ok := m.backend.Remote(r.Path); ok && a.IsValid() {
			v.Observed = netip.AddrPortFrom(a.Addr().Unmap(), a.Port()).String()
		}
		if d := m.directs[r.Peer]; d != nil {
			d.remoteUp = v.Direct && d.link != nil
			v.Direct = d.local
		}
		if m.cfg.Role == "relay" && slices.Contains(m.store.state.Authorized[r.Peer], m.cfg.Self) {
			owner, ok := m.owners.owner(r.Peer, now)
			v.Reclaim = !in.prepared || !ok || (owner == m.cfg.Self && in.sequence == 0)
		} else if m.linked(r.Peer) {
			// The dialing side prepares a link once; after this side restarts it
			// has to be asked again, or this side never commits the path.
			v.Reclaim = !in.prepared
		}
		m.send(r.Peer, r.Path, v, 0)
		m.routePinged(r.Peer, v.Nonce, now)
	case "pong":
		p := m.match(r.Path, r.Peer)
		if p == nil && m.trialPong(r, v, now) {
			return
		}
		if p == nil || p.token != v.Token || p.path.Generation != v.Generation || p.pending[v.Probe] != v.Nonce {
			return
		}
		swapping := p.swapping(now)
		if !v.Reclaim && swapping && p.trend.sawReclaim && !p.reprepare {
			m.settledAfterRedial(p)
		}
		if v.Reclaim {
			if swapping {
				// A redial's new connection: prepared again while the
				// routes stay on the path.
				m.reclaimAfterRedial(p)
			} else {
				if p.routeReady {
					p.since = now
				}
				p.routeReady = false
			}
			if m.proposal == nil && p.config.ID == m.active {
				if seq, err := m.store.nextSequence(); err == nil {
					m.proposal = newProposal(p, seq, "entry requested renewed ownership", !p.ready || p.track.stats.Failures >= m.cfg.Parameters.ConsecutiveFailures, now)
				}
			}
		}
		if d := m.directOf(p); d != nil {
			d.remoteUp = v.Direct
		}
		if m.candidates[p.config.ID] == p {
			m.observedBy(p.config.Peer, v.Observed, now)
		}
		if v.Echo != "" && len(v.Echo) <= 64 {
			if p.echoes == nil {
				p.echoes = map[uint64]string{}
			}
			p.echoes[v.Probe] = v.Echo
		}
		if pending, ok := p.track.pending[v.Probe]; ok && pending.written.IsZero() {
			p.early[v.Probe] = now
		} else {
			m.success(p, v.Probe, now)
		}
	case "seen":
		in := m.inbound[r.Path]
		if !matches(in, r.Peer, v) {
			return
		}
		at, ok := in.challenges[v.Nonce]
		if !ok || now.Sub(at) > m.cfg.Parameters.MaxTimeout || (v.Echo != "" && v.Echo != in.echoes[v.Nonce]) {
			return
		}
		delete(in.challenges, v.Nonce)
		delete(in.echoes, v.Nonce)
		in.lastSeen = now
		// The prober knows its ping's nonce before the pong comes, the echo
		// only from it: only a "seen" that repeats it is a round trip. Every
		// node of this protocol repeats it.
		if v.Echo == "" {
			return
		}
		m.routeSeen(r.Peer, v.Nonce, now)
	case "prepare":
		in := m.inbound[r.Path]
		if !matches(in, r.Peer, v) || now.Sub(in.lastSeen) > m.cfg.Parameters.MaxTimeout {
			return
		}
		if !m.authorizedPeer(r.Peer) || !m.backend.DataReady(r.Peer, r.Path) {
			return
		}
		in.prepared = true
		v.Type = "ready"
		m.send(r.Peer, r.Path, v, 0)
		// Each member of a punched link commits the path it probes itself;
		// the peer's may be another one.
		if m.linked(r.Peer) && m.directs[r.Peer] == nil {
			if err = m.backend.Commit(r.Peer, r.Path); err != nil {
				m.emit("path.failed", "link:"+r.Peer+": commit failed")
			}
		}
		m.followPrepared(r.Peer, r.Path, now)
	case "ready":
		p := m.match(r.Path, r.Peer)
		if p == nil || p.token != v.Token || p.path.Generation != v.Generation || !p.ready || !m.backend.DataReady(r.Peer, r.Path) {
			return
		}
		p.routeReady = true
		p.reprepare = false
		m.pathReady(p)
		if m.links[r.Peer] == p || m.directOf(p) != nil {
			if err = m.backend.Commit(r.Peer, r.Path); err != nil {
				m.pathFailed(p, "commit failed")
			}
		}
	case "commit":
		m.acceptCommit(r, v, now)
	case "committed":
		m.finishCommit(r, v, now)
	case "conflict":
		m.acceptConflict(r, v, now)
	case "owner":
		m.acceptNotice(r, v, now)
	case "offer":
		m.receiveOffer(r, v, now)
	case "punch":
		m.receivePunch(r, v, now)
	case msgAdvert:
		m.receiveAdvert(r, v, now)
	case "reach", "reach-relay", "reach-ack":
		m.receiveReach(r, v, now)
	case "owner-ack":
		if v.Notice == nil {
			return
		}
		key := r.Peer + ":" + v.Notice.Member
		// The base fields name the notice; the signatures are not repeated
		// by every sender alike.
		if n, ok := m.notices[key]; ok && n.notice.base() == v.Notice.base() {
			if n.notice.Refresh == 1 {
				m.emit("owner.ack", fmt.Sprintf("%s member=%s seq=%d", r.Peer, n.notice.Member, n.notice.Seq))
			}
			delete(m.notices, key)
		}
	}
}

func matches(in *inbound, peer string, v message) bool {
	return in != nil && in.peer == peer && in.token == v.Token && in.generation == v.Generation
}
func (m *Manager) match(path Path, peer string) *candidateState {
	for _, p := range m.allPaths() {
		if p.path == path && p.config.Peer == peer {
			return p
		}
	}
	return nil
}
func (m *Manager) linked(peer string) bool {
	return slices.ContainsFunc(m.cfg.Links, func(l Link) bool { return l.Peer == peer })
}
func (m *Manager) authorizedPeer(peer string) bool {
	if m.linked(peer) {
		return true
	}
	return m.cfg.Role == "relay" && slices.Contains(m.store.state.Authorized[peer], m.cfg.Self)
}
func (m *Manager) peerPath(peer string) (Path, bool) {
	if p := m.links[peer]; p != nil && p.ready && p.path.ID != "" {
		return p.path, true
	}
	if d := m.directs[peer]; d != nil && d.link != nil && d.state == directUp && d.link.path.ID != "" {
		return d.link.path, true
	}
	for _, in := range m.inbound {
		if in.peer == peer && in.prepared && !in.lastSeen.IsZero() && time.Since(in.lastSeen) < m.cfg.Parameters.Lease {
			return in.path, true
		}
	}
	return Path{}, false
}
func (m *Manager) acceptCommit(r Received, v message, now time.Time) {
	in := m.inbound[r.Path]
	if !matches(in, r.Peer, v) || !in.prepared || now.Sub(in.lastSeen) > m.cfg.Parameters.MaxTimeout || m.cfg.Role != "relay" || !slices.Contains(m.store.state.Authorized[r.Peer], m.cfg.Self) || v.Sequence == 0 {
		return
	}
	if !m.verified(claimInput(m.cfg.Network, r.Peer, m.cfg.Self, v.Sequence).VerifyString(m.signKeys[r.Peer], v.MSig), "commit", r.Peer, r.Peer) {
		return
	}
	old := m.store.state.Claims[r.Peer]
	if old.Seq > v.Sequence || (old.Seq == v.Sequence && (old.Owner != m.cfg.Self || old.Conflict)) {
		// The member raises its sequence only past one it signed itself.
		m.send(r.Peer, r.Path, message{Type: "conflict", Sequence: old.Seq, Nonce: v.Nonce, Token: v.Token, Generation: v.Generation,
			Claim: &SignedClaim{Owner: old.Owner, Seq: old.Seq, MSig: old.MSig}}, 0)
		return
	}
	if err := m.backend.Commit(r.Peer, r.Path); err != nil {
		m.emit("owner.error", fmt.Sprintf("%s seq=%d: commit failed: %v", r.Peer, v.Sequence, err))
		return
	}
	in.sequence = v.Sequence
	n := m.notice(r.Peer, v.Sequence, old.Refresh+1, v.MSig)
	if _, err := m.owners.accept(m.cfg.Self, n, now); err != nil {
		m.emit("owner.error", fmt.Sprintf("%s seq=%d: %v", r.Peer, v.Sequence, err))
		return
	}
	if err := m.routes(); err != nil {
		return
	}
	m.broadcast(n, "")
	m.lastRefresh[r.Peer] = now
	v.Type = "committed"
	m.send(r.Peer, r.Path, v, 0)
}

// acceptConflict takes a relay's answer to the proposal's commit that its
// sequence is not the newest it knows, and moves the sequence past it. Only
// the relay the commit went to, on the path it went over, answers it,
// repeating the commit's nonce, token and generation, and only with a claim
// this member signed itself: no relay moves a member's sequence elsewhere.
func (m *Manager) acceptConflict(r Received, v message, now time.Time) {
	q := m.proposal
	if q == nil || v.Claim == nil || v.Claim.Seq < q.sequence || !now.Before(q.expires) || q.path != r.Path || q.token != v.Token {
		return
	}
	p := m.candidates[q.candidate]
	if p == nil || p.config.Peer != r.Peer || p.path != r.Path {
		return
	}
	if v.Nonce != q.nonce || v.Token != p.token || v.Generation != p.path.Generation {
		return
	}
	if !m.verified(claimInput(m.cfg.Network, m.cfg.Self, v.Claim.Owner, v.Claim.Seq).VerifyString(m.signKeys[m.cfg.Self], v.Claim.MSig), "conflict claim", m.cfg.Self, r.Peer) {
		return
	}
	m.error = "ownership conflict requires higher sequence"
	s := m.store.copy()
	s.Sequence = max(s.Sequence, v.Claim.Seq)
	if m.store.commit(s) == nil {
		seq, e := m.store.nextSequence()
		if e == nil {
			q.sequence = seq
			q.next = now
		}
	}
}

func (m *Manager) finishCommit(r Received, v message, now time.Time) {
	q := m.proposal
	if q == nil || q.sequence != v.Sequence || q.nonce != v.Nonce || !now.Before(q.expires) || q.path != r.Path || q.token != v.Token {
		return
	}
	p := m.candidates[q.candidate]
	if p == nil || p.config.Peer != r.Peer || p.path != r.Path || v.Token != p.token || v.Generation != p.path.Generation {
		return
	}
	if err := m.backend.Commit(r.Peer, r.Path); err != nil {
		m.pathFailed(p, "commit failed")
		return
	}
	old := m.active
	m.active = q.candidate
	if err := m.routes(); err != nil {
		return
	}
	if old != "" && old != m.active {
		if oldPath := m.candidates[old]; oldPath != nil && oldPath.config.Peer != p.config.Peer {
			m.backend.Deactivate(oldPath.config.Peer, oldPath.path)
			oldPath.since = now.Add(-8 * time.Second)
		}
		m.switches++
		m.switchTimes = append(m.switchTimes, now)
		m.emit("entry.switch", fmt.Sprintf("%s -> %s: %s", old, m.active, q.reason))
	}
	m.since = now
	m.emit("entry.committed", fmt.Sprintf("%s seq=%d", m.active, q.sequence))
	m.proposal = nil
	m.error = ""
	keep := m.switchTimes[:0]
	for _, t := range m.switchTimes {
		if now.Sub(t) <= 5*time.Minute {
			keep = append(keep, t)
		}
	}
	m.switchTimes = keep
	if len(keep) >= 3 {
		m.emit("entry.flapping", "at least 3 entry switches in 5 minutes")
	}
}

// notice is this relay's signed lease of member's claim at seq, which the
// member signed as msig.
func (m *Manager) notice(member string, seq, refresh uint64, msig string) Notice {
	n := Notice{Member: member, Owner: m.cfg.Self, Seq: seq, Refresh: refresh, Generation: m.cfg.Revision, LeaseMs: m.cfg.Parameters.Lease.Milliseconds(), MSig: msig}
	n.OSig = noticeInput(m.cfg.Network, n).SignString(m.cfg.SignKey)
	return n
}

func (m *Manager) broadcast(n Notice, except string) {
	now := time.Now()
	for _, l := range m.cfg.Links {
		// A punched relay link not up gets the claims with the next refresh
		// after it is.
		if !m.owners.relays[l.Peer] || l.Peer == except || (l.Punch && !m.directUp(l.Peer)) {
			continue
		}
		key := l.Peer + ":" + n.Member
		since := now
		if old, ok := m.notices[key]; ok {
			since = old.since
		}
		m.notices[key] = outboundNotice{notice: n, peer: l.Peer, next: now, expires: now.Add(time.Duration(n.LeaseMs) * time.Millisecond), since: since}
	}
}
func (m *Manager) pendingNotices(now time.Time) int {
	n := 0
	for _, o := range m.notices {
		if now.Sub(o.since) >= noticeGrace {
			n++
		}
	}
	return n
}
func (m *Manager) acceptNotice(r Received, v message, now time.Time) {
	if v.Notice == nil || !m.linked(r.Peer) {
		return
	}
	n := *v.Notice
	changed, err := m.owners.accept(r.Peer, n, now)
	if errors.Is(err, errReplay) {
		if old, ok := m.store.state.Claims[n.Member]; ok && old.Owner == n.Owner && old.Seq == n.Seq && old.Refresh == n.Refresh {
			m.send(r.Peer, r.Path, message{Type: "owner-ack", Notice: &n}, 0)
		}
		return
	}
	if err != nil {
		if errors.Is(err, errConflict) {
			m.emit("owner.conflict", n.Member)
		}
		if errors.Is(err, errSignature) {
			m.verified(false, "owner notice", n.Member+"/"+n.Owner, r.Peer)
		}
		return
	}
	if changed {
		m.emit("owner.changed", fmt.Sprintf("%s -> %s seq=%d", n.Member, n.Owner, n.Seq))
		if m.routes() != nil {
			return
		}
		if n.Owner != m.cfg.Self {
			for _, in := range m.inbound {
				if in.peer == n.Member && in.sequence > 0 {
					m.backend.Deactivate(in.peer, in.path)
					in.sequence = 0
				}
			}
		}
	}
	m.send(r.Peer, r.Path, message{Type: "owner-ack", Notice: &n}, 0)
	m.broadcast(n, r.Peer)
}
func (m *Manager) refresh(now time.Time) {
	if m.cfg.Role != "relay" {
		return
	}
	for member, c := range m.store.state.Claims {
		if c.Owner != m.cfg.Self || c.Conflict || now.Sub(m.lastRefresh[member]) < m.cfg.Parameters.LeaseRefresh {
			continue
		}
		var live *inbound
		for _, in := range m.inbound {
			if in.peer == member && in.sequence == c.Seq && in.lastSeen.After(m.lastRefresh[member]) && now.Sub(in.lastSeen) < m.cfg.Parameters.LeaseRefresh {
				live = in
				break
			}
		}
		if live == nil {
			continue
		}
		n := m.notice(member, c.Seq, c.Refresh+1, c.MSig)
		if _, err := m.owners.accept(m.cfg.Self, n, now); err != nil {
			m.emit("owner.error", fmt.Sprintf("%s seq=%d refresh: %v", member, c.Seq, err))
			continue
		}
		m.lastRefresh[member] = now
		m.broadcast(n, "")
	}
}
