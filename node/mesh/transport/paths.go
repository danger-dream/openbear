package transport

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"net/url"
	"strconv"
	"strings"
	"time"

	"golang.zx2c4.com/wireguard/conn"
	"openbear/node/mesh/wire"
)

var ErrPathChanged = errors.New("active path changed")

type PeerIdentity struct {
	ID                  string
	DataKey, ControlKey [32]byte
}
type PathRef struct {
	ID         string
	Generation uint64
}
type PathInfo struct {
	Ref                                                                       PathRef
	PeerID, Endpoint, Transport, LocalAddr, RemoteAddr                        string
	Plane                                                                     uint8
	ReceivedAt                                                                time.Time
	LastAuthenticated                                                         time.Time
	AuthenticatedPackets                                                      uint64
	LastError                                                                 string
	Connected, Authenticated, DataAuthenticated, ControlAuthenticated, Active bool
}
type PathEvent struct {
	Kind                             string
	Path                             PathInfo
	SendID                           uint64
	WriteStartedAt, WriteCompletedAt time.Time
	Err                              error
}
type ResourceStats struct {
	Connections, Unauthenticated, SocketBufferBytes, ReceiveBytes, ControlReceiveBytes int
	DroppedEvents                                                                      uint64
}
type peerPaths struct {
	identity          PeerIdentity
	active, bootstrap PathRef
	// protocol is the peer's mesh protocol range as SetPeerProtocol last
	// gave it; zero while unknown.
	protocol wire.Range
}

type pathEndpoint struct {
	*endpoint
	ref        PathRef
	plane      uint8
	receivedAt time.Time
	// session is the stream connection a packet came on: after a redial a
	// path has two for a while.
	session *session
}
type logicalEndpoint struct {
	b    *Bind
	peer string
}

func (b *Bind) initPath(ep *endpoint) {
	if ep.id == "" {
		b.nextID++
		ep.id = "p" + strconv.FormatUint(b.nextID, 36)
	}
}
func (b *Bind) newGeneration(ep *endpoint) {
	b.initPath(ep)
	ep.generation++
	ep.auth = [2]bool{}
	ep.lastAuthenticated = [2]time.Time{}
	ep.authenticatedPackets = [2]uint64{}
	ep.stats.Authenticated = false
	b.ids[ep.id] = ep
	// The control plane falls back to the bootstrap path, which is never committed again
	// when it is a backup: it follows the path to its new connection.
	if p := b.peers[ep.peer]; p != nil && p.bootstrap.ID == ep.id {
		p.bootstrap.Generation = ep.generation
	}
}
func (b *Bind) info(ep *endpoint, plane uint8, at time.Time) PathInfo {
	ref := PathRef{ep.id, ep.generation}
	p := b.peers[ep.peer]
	connected := !ep.retired && b.run != nil && ((ep.transport == "udp" && b.run.udp != nil) || (ep.current != nil && ep.current.ready && !ep.current.closed.Load()))
	return PathInfo{LastAuthenticated: ep.lastAuthenticated[plane], AuthenticatedPackets: ep.authenticatedPackets[plane], LastError: ep.stats.LastError, Ref: ref, PeerID: ep.peer, Endpoint: ep.key, Transport: ep.transport, LocalAddr: ep.stats.LocalAddr, RemoteAddr: ep.stats.RemoteAddr, Plane: plane, ReceivedAt: at, Connected: connected, Authenticated: ep.auth[plane], DataAuthenticated: ep.auth[0], ControlAuthenticated: ep.auth[1], Active: p != nil && p.active == ref}
}
func (b *Bind) event(e PathEvent) {
	select {
	case b.events <- e:
	default:
		b.droppedEvents++
	}
}
func (b *Bind) Events() <-chan PathEvent { return b.events }
func (b *Bind) Resources() ResourceStats {
	b.mu.Lock()
	defer b.mu.Unlock()
	s := ResourceStats{DroppedEvents: b.droppedEvents}
	if r := b.run; r != nil {
		s.Connections = len(r.sessions)
		s.Unauthenticated = r.unauth
		s.SocketBufferBytes = r.socketBytes
		s.ReceiveBytes = r.queuedBytes
		s.ControlReceiveBytes = r.controlBytes
	}
	return s
}
func (b *Bind) Paths() []PathInfo {
	b.mu.Lock()
	defer b.mu.Unlock()
	out := make([]PathInfo, 0, len(b.ids))
	for _, ep := range b.ids {
		out = append(out, b.info(ep, 0, time.Time{}))
	}
	return out
}
func (b *Bind) PathInfo(e conn.Endpoint) (PathInfo, bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	p, ok := e.(*pathEndpoint)
	if !ok || p.b != b || p.retired || p.generation != p.ref.Generation {
		return PathInfo{}, false
	}
	return b.info(p.endpoint, p.plane, p.receivedAt), true
}
func (b *Bind) RegisterPeer(identity PeerIdentity) error {
	if identity.ID == "" || len(identity.ID) > 256 || identity.DataKey == ([32]byte{}) || identity.ControlKey == ([32]byte{}) || identity.DataKey == identity.ControlKey {
		return errors.New("invalid peer identity")
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if p := b.peers[identity.ID]; p != nil {
		if p.identity == identity {
			return nil
		}
		return errors.New("remove peer before replacing its keys")
	}
	if len(b.peers) >= b.opt.limits().paths {
		return ErrBackpressure
	}
	for _, p := range b.peers {
		if p.identity.DataKey == identity.DataKey || p.identity.ControlKey == identity.ControlKey || p.identity.DataKey == identity.ControlKey || p.identity.ControlKey == identity.DataKey {
			return errors.New("peer keys already registered")
		}
	}
	b.managed = true
	b.peers[identity.ID] = &peerPaths{identity: identity}
	return nil
}
func (b *Bind) PeerEndpoint(id string) (string, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.peers[id] == nil {
		return "", errors.New("unknown peer")
	}
	return "mesh-peer://" + url.PathEscape(id), nil
}
func (b *Bind) parseLogical(s string) (conn.Endpoint, error) {
	id, err := url.PathUnescape(strings.TrimPrefix(s, "mesh-peer://"))
	if err != nil {
		return nil, err
	}
	if _, err = b.PeerEndpoint(id); err != nil {
		return nil, err
	}
	return &logicalEndpoint{b: b, peer: id}, nil
}
func (b *Bind) Prepare(ctx context.Context, id, uri string) (result PathRef, resultErr error) {
	if err := ctx.Err(); err != nil {
		return PathRef{}, err
	}
	b.mu.Lock()
	known := b.peers[id] != nil
	b.mu.Unlock()
	if !known {
		return PathRef{}, errors.New("unknown peer")
	}
	key, _, _, keyErr := endpointKey(uri)
	if keyErr != nil {
		return PathRef{}, keyErr
	}
	b.mu.Lock()
	count := 0
	for _, ep := range b.paths {
		if ep.peer == id {
			count++
		}
	}
	if b.run != nil {
		for ss := range b.run.sessions {
			if !ss.ep.outbound && ss.ep.peer == id {
				count++
			}
		}
	}
	full := b.paths[key] == nil && count >= 3 && !b.evictStale(id, nil)
	if ep := b.paths[key]; ep != nil && !ep.outbound && ep.transport == "udp" && ep.peer == id && !ep.retired {
		// An address the peer's packets came from becomes a prepared path,
		// keeping its generation and what it has authenticated.
		ep.outbound = true
		ep.stats.Outbound = true
		ep.wake = make(chan struct{}, 1)
	}
	b.mu.Unlock()
	if full {
		return PathRef{}, ErrBackpressure
	}
	e, err := b.ParseEndpoint(uri)
	if err != nil {
		return PathRef{}, err
	}
	ep, ok := e.(*endpoint)
	if !ok {
		return PathRef{}, errors.New("physical endpoint required")
	}
	b.mu.Lock()
	if ep.peer != "" && ep.peer != id {
		b.mu.Unlock()
		return PathRef{}, errors.New("path belongs to another peer")
	}
	if b.peers[id] == nil {
		b.mu.Unlock()
		b.Release(ep.key)
		return PathRef{}, net.ErrClosed
	}
	if ep.peer == "" {
		count := 0
		for _, other := range b.paths {
			if other != ep && other.peer == id {
				count++
			}
		}
		if b.run != nil {
			for ss := range b.run.sessions {
				if !ss.ep.outbound && ss.ep.peer == id {
					count++
				}
			}
		}
		if count >= 3 && !b.evictStale(id, ep) {
			ep.retired = true
			b.mu.Unlock()
			b.op.Lock()
			b.releaseTargets(map[*endpoint]bool{ep: true})
			b.op.Unlock()
			return PathRef{}, ErrBackpressure
		}
	}
	ep.peer = id
	ep.preparing++
	b.mu.Unlock()
	defer func() {
		b.op.Lock()
		defer b.op.Unlock()
		b.mu.Lock()
		ep.preparing--
		if resultErr == nil {
			ep.prepared = true
		}
		cleanup := resultErr != nil && ep.preparing == 0 && !ep.prepared && !ep.retired
		for _, p := range b.peers {
			if p.active.ID == ep.id {
				cleanup = false
			}
		}
		if cleanup {
			ep.retired = true
		}
		b.mu.Unlock()
		if cleanup {
			b.releaseTargets(map[*endpoint]bool{ep: true})
		}
	}()
	ticker := time.NewTicker(10 * time.Millisecond)
	defer ticker.Stop()
	for {
		b.mu.Lock()
		p := b.peers[id]
		info := b.info(ep, 0, time.Time{})
		state := ep.stats.State
		last := ep.stats.LastError
		closed := ep.retired || b.run == nil || p == nil
		if !closed && info.Connected {
			if p.bootstrap.ID == "" {
				p.bootstrap = info.Ref
			}
			b.mu.Unlock()
			return info.Ref, nil
		}
		b.mu.Unlock()
		if closed {
			return PathRef{}, net.ErrClosed
		}
		if state == "security_error" {
			return PathRef{}, fmt.Errorf("%w: %s", ErrSecurity, last)
		}
		select {
		case <-ctx.Done():
			return PathRef{}, ctx.Err()
		case <-ticker.C:
		}
	}
}
func (b *Bind) path(ref PathRef) (*endpoint, error) {
	ep := b.ids[ref.ID]
	if ep == nil || ep.retired || ep.generation != ref.Generation {
		return nil, net.ErrClosed
	}
	return ep, nil
}
func (b *Bind) PathEndpoint(ref PathRef) (conn.Endpoint, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	ep, err := b.path(ref)
	if err != nil {
		return nil, err
	}
	return &pathEndpoint{endpoint: ep, ref: ref}, nil
}
func (b *Bind) Commit(id string, ref PathRef) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	ep, err := b.path(ref)
	if err != nil {
		return err
	}
	p := b.peers[id]
	if p == nil || ep.peer != id || !ep.auth[1] || !b.info(ep, 1, time.Time{}).Connected {
		return errors.New("path is not authenticated for this peer")
	}
	p.active = ref
	p.bootstrap = ref
	b.event(PathEvent{Kind: "committed", Path: b.info(ep, 0, time.Time{})})
	return nil
}
func (b *Bind) Deactivate(id string, expected PathRef) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	p := b.peers[id]
	if p == nil {
		return net.ErrClosed
	}
	if expected.ID == "" || expected.Generation == 0 || p.active != expected {
		return ErrPathChanged
	}
	p.active = PathRef{}
	if p.bootstrap == expected {
		p.bootstrap = PathRef{}
	}
	info := PathInfo{PeerID: id, Ref: expected}
	if ep, err := b.path(expected); err == nil {
		info = b.info(ep, 0, time.Time{})
	}
	b.event(PathEvent{Kind: "deactivated", Path: info})
	return nil
}

func (b *Bind) ReleasePath(ref PathRef) error {
	b.op.Lock()
	defer b.op.Unlock()
	b.mu.Lock()
	if b.ids[ref.ID] == nil {
		b.mu.Unlock()
		return nil
	}
	ep, err := b.path(ref)
	if err != nil {
		b.mu.Unlock()
		return err
	}
	for _, p := range b.peers {
		if p.active.ID == ref.ID {
			b.mu.Unlock()
			return errors.New("cannot release active path")
		}
	}
	ep.retired = true
	b.mu.Unlock()
	b.releaseTargets(map[*endpoint]bool{ep: true})
	return nil
}
func (b *Bind) RemovePeer(id string) error {
	b.op.Lock()
	defer b.op.Unlock()
	b.mu.Lock()
	delete(b.peers, id)
	targets := map[*endpoint]bool{}
	for _, ep := range b.paths {
		if ep.peer == id {
			targets[ep] = true
		}
	}
	if b.run != nil {
		for ss := range b.run.sessions {
			if ss.ep.peer == id {
				targets[ss.ep] = true
			}
		}
	}
	for ep := range targets {
		ep.retired = true
		delete(b.pins, ep.key)
	}
	b.mu.Unlock()
	b.releaseTargets(targets)
	return nil
}

// staleAfter is how long an address a peer's packets came from has to have
// been silent before a new one may take its place. Health probes keep a
// path in use authenticated every second or two.
const staleAfter = 30 * time.Second

// evictStale makes room for one more path of peer id, which has three
// already: the address it was heard from longest ago goes, if that was
// more than staleAfter ago and it is not the path in use. A peer behind a
// NAT that gives it a new public address, as a PPPoE line does at every
// redial, leaves its old addresses here; without this its fourth was
// refused until this node restarted. Called with b.mu held.
func (b *Bind) evictStale(id string, keep *endpoint) bool {
	p := b.peers[id]
	var oldest *endpoint
	var oldestAt time.Time
	for _, ep := range b.ids {
		if ep == keep || ep.peer != id || ep.outbound || ep.transport != "udp" || ep.retired || ep.preparing > 0 ||
			p != nil && (p.active.ID == ep.id || p.bootstrap.ID == ep.id) {
			continue
		}
		at := ep.lastAuthenticated[0]
		if ep.lastAuthenticated[1].After(at) {
			at = ep.lastAuthenticated[1]
		}
		if oldest == nil || at.Before(oldestAt) {
			oldest, oldestAt = ep, at
		}
	}
	if oldest == nil || time.Since(oldestAt) < staleAfter {
		return false
	}
	oldest.retired = true
	delete(b.ids, oldest.id)
	if b.paths[oldest.key] == oldest {
		delete(b.paths, oldest.key)
	}
	return true
}

func (b *Bind) authenticated(plane uint8, m conn.ReceiveMetadata) bool {
	p, ok := m.Endpoint.(*pathEndpoint)
	if !ok || p.b != b {
		return false
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	ep := p.endpoint
	if b.run == nil || ep.retired || ep.generation != p.ref.Generation || !b.info(ep, plane, time.Time{}).Connected {
		return false
	}
	peerID := ""
	if b.managed {
		for id, v := range b.peers {
			key := v.identity.DataKey
			if plane == 1 {
				key = v.identity.ControlKey
			}
			if key == m.PublicKey {
				peerID = id
				break
			}
		}
		if peerID == "" || (ep.peer != "" && ep.peer != peerID) {
			return false
		}
		if ep.peer == "" {
			count := 0
			for _, other := range b.ids {
				if other != ep && other.peer == peerID {
					count++
				}
			}
			if count >= 3 && !b.evictStale(peerID, ep) {
				return false
			}
		}
		ep.peer = peerID
	}
	if ep.transport == "udp" {
		if existing := b.paths[ep.key]; existing != nil && existing != ep {
			return false
		}
		if b.paths[ep.key] == nil {
			if len(b.paths) >= b.opt.limits().paths {
				return false
			}
			b.paths[ep.key] = ep
			b.ids[ep.id] = ep
		}
	}
	first := !ep.auth[plane]
	ep.auth[plane] = true
	ep.lastAuthenticated[plane] = time.Now()
	ep.authenticatedPackets[plane]++
	ep.stats.Authenticated = true
	ep.stats.State = "ready"
	ss := p.session
	if ss == nil {
		ss = ep.current
	}
	if ss != nil && !ss.authenticated {
		ss.authenticated = true
		if !ep.outbound {
			b.run.unauth--
		}
	}
	if first {
		b.event(PathEvent{Kind: "authenticated", Path: b.info(ep, plane, p.receivedAt)})
	}
	if peer := b.peers[peerID]; peer != nil && peer.bootstrap.ID == "" {
		peer.bootstrap = p.ref
	}
	return true
}
func (b *Bind) AuthenticatedReceive(m conn.ReceiveMetadata) bool { return b.authenticated(0, m) }

func (e *logicalEndpoint) ClearSrc()           {}
func (e *logicalEndpoint) DstToString() string { return "mesh-peer://" + url.PathEscape(e.peer) }
func (e *logicalEndpoint) DstToBytes() []byte  { return []byte(e.DstToString()) }
func (e *logicalEndpoint) SrcToString() string { return "" }
func (e *logicalEndpoint) SrcIP() netip.Addr   { return netip.Addr{} }
func (e *logicalEndpoint) DstIP() netip.Addr {
	e.b.mu.Lock()
	defer e.b.mu.Unlock()
	if p := e.b.peers[e.peer]; p != nil {
		if ep, _ := e.b.path(p.active); ep != nil {
			return ep.remote.Addr()
		}
		if ep, _ := e.b.path(p.bootstrap); ep != nil {
			return ep.remote.Addr()
		}
	}
	return netip.Addr{}
}
