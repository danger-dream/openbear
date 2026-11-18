package fast

import (
	"context"
	"errors"
	"maps"
	"math"
	"net"
	"net/netip"
	"slices"
	"sync"
	"time"

	"openbear/node/mesh/bypass"
)

const (
	headerWait      = 60 * time.Second
	poolMin         = 2
	poolMax         = 16
	poolMaxAge      = 45 * time.Second
	poolIdle        = 5 * time.Minute
	peerDialTimeout = 3 * time.Second
	peerDownHold    = 5 * time.Second
)

type pooledConn struct {
	c       *net.TCPConn
	at      time.Time
	target  string
	viaMesh bool
}

type peerTarget struct {
	direct string
	mesh   string
}

type peerPool struct {
	idle    []pooledConn
	want    int
	used    time.Time
	dialing int
	downAt  time.Time
	err     string
	target  string
	viaMesh bool
	rtt     time.Duration
}

func (e *peerPool) sample(took time.Duration) {
	if e.rtt == 0 {
		e.rtt = took
	} else {
		e.rtt = (e.rtt*7 + took*3) / 10
	}
}

type pool struct {
	mu      sync.Mutex
	targets map[string]peerTarget
	peers   map[string]*peerPool
	closed  bool
	stop    chan struct{}
	ctx     context.Context
	cancel  context.CancelFunc
	dial    func(ctx context.Context, addr string) (*net.TCPConn, error)
}

func newPool() *pool {
	p := &pool{
		targets: map[string]peerTarget{},
		peers:   map[string]*peerPool{},
		stop:    make(chan struct{}),
	}
	p.ctx, p.cancel = context.WithCancel(context.Background())
	go p.maintain()
	return p
}

func (p *pool) close() {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.closed {
		return
	}
	p.closed = true
	p.cancel()
	close(p.stop)
	for _, e := range p.peers {
		for _, x := range e.idle {
			_ = x.c.Close()
		}
		e.idle = nil
	}
}

func (p *pool) configure(c *Config, selfID string) {
	next := map[string]peerTarget{}
	if c != nil {
		for id, addr := range c.Nodes {
			if id == selfID || !addr.IsValid() {
				continue
			}
			next[id] = peerTarget{
				direct: c.Next[id],
				mesh:   netip.AddrPortFrom(addr, uint16(c.peerMeshPort(id))).String(),
			}
		}
	}
	p.mu.Lock()
	old := p.targets
	p.targets = maps.Clone(next)
	for id, e := range p.peers {
		nt, ok := next[id]
		if !ok || old[id] != nt {
			for _, x := range e.idle {
				_ = x.c.Close()
			}
			e.idle = nil
			if !ok {
				delete(p.peers, id)
			}
		}
	}
	p.mu.Unlock()
}

func (p *pool) entry(peer string) *peerPool {
	e := p.peers[peer]
	if e == nil {
		e = &peerPool{want: poolMin}
		p.peers[peer] = e
	}
	return e
}

// peerKeepAlive finds a connection between nodes gone silent while idle, a
// pooled one above all, in about 15 seconds.
var peerKeepAlive = net.KeepAliveConfig{Enable: true, Idle: 5 * time.Second, Interval: 3 * time.Second, Count: 3}

func (p *pool) dialAddr(ctx context.Context, addr string) (*net.TCPConn, error) {
	if p.dial != nil {
		return p.dial(ctx, addr)
	}
	d := &net.Dialer{
		Timeout:         peerDialTimeout,
		KeepAliveConfig: peerKeepAlive,
		Control:         bypass.Control,
	}
	var c net.Conn
	err := withFastSocket(func() error {
		var e error
		c, e = d.DialContext(ctx, "tcp", addr)
		return e
	})
	if err != nil {
		return nil, err
	}
	tc := c.(*net.TCPConn)
	_ = tc.SetNoDelay(true)
	limitRetransmit(tc)
	return tc, nil
}

// dialPeer tries the peer's direct fast endpoint first, falling back to its
// mesh address on the fast port when unreachable or when only mesh is configured.
func (p *pool) dialPeer(peer string) (*net.TCPConn, string, bool, error) {
	p.mu.Lock()
	t, ok := p.targets[peer]
	ok = ok && !p.closed
	p.mu.Unlock()
	if !ok {
		return nil, "", false, errors.New("unknown fast peer " + peer)
	}
	if t.direct != "" {
		ctx, cancel := context.WithTimeout(p.ctx, peerDialTimeout)
		c, err := p.dialAddr(ctx, t.direct)
		cancel()
		if err == nil {
			return c, t.direct, false, nil
		}
		if t.mesh == "" || t.mesh == t.direct {
			return nil, t.direct, false, err
		}
	}
	if t.mesh == "" {
		return nil, "", false, errors.New("peer has no address")
	}
	ctx, cancel := context.WithTimeout(p.ctx, peerDialTimeout)
	defer cancel()
	c, err := p.dialAddr(ctx, t.mesh)
	return c, t.mesh, true, err
}

func (p *pool) get(peer string) (*net.TCPConn, error) {
	if p == nil {
		return nil, net.ErrClosed
	}
	p.mu.Lock()
	if p.closed {
		p.mu.Unlock()
		return nil, net.ErrClosed
	}
	if _, ok := p.targets[peer]; !ok {
		p.mu.Unlock()
		return nil, errors.New("unknown fast peer " + peer)
	}
	e := p.entry(peer)
	e.used = time.Now()
	var c *net.TCPConn
	for len(e.idle) > 0 && c == nil {
		x := e.idle[len(e.idle)-1]
		e.idle = e.idle[:len(e.idle)-1]
		if time.Since(x.at) < poolMaxAge && connAlive(x.c) {
			c = x.c
			e.target, e.viaMesh = x.target, x.viaMesh
		} else {
			_ = x.c.Close()
		}
	}
	if len(e.idle) == 0 {
		e.want = min(e.want*2, poolMax)
	}
	p.mu.Unlock()
	p.fill(peer)
	if c != nil {
		return c, nil
	}
	start := time.Now()
	c, target, viaMesh, err := p.dialPeer(peer)
	p.mu.Lock()
	if p.closed {
		p.mu.Unlock()
		if c != nil {
			c.Close()
		}
		return nil, net.ErrClosed
	}
	if cur := p.peers[peer]; cur != nil {
		if err != nil {
			cur.downAt, cur.err, cur.target = time.Now(), err.Error(), target
		} else {
			cur.downAt, cur.err = time.Time{}, ""
			cur.target, cur.viaMesh = target, viaMesh
			cur.sample(time.Since(start))
		}
	}
	p.mu.Unlock()
	return c, err
}

func connAlive(c *net.TCPConn) bool {
	_ = c.SetReadDeadline(time.Now())
	var b [1]byte
	_, err := c.Read(b[:])
	_ = c.SetReadDeadline(time.Time{})
	var ne net.Error
	return errors.As(err, &ne) && ne.Timeout()
}

func (p *pool) fill(peer string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.closed {
		return
	}
	if _, ok := p.targets[peer]; !ok {
		return
	}
	e := p.entry(peer)
	if !e.downAt.IsZero() && time.Since(e.downAt) < peerDownHold {
		return
	}
	for n := e.want - len(e.idle) - e.dialing; n > 0; n-- {
		e.dialing++
		go p.add(peer, e)
	}
}

func (p *pool) add(peer string, e *peerPool) {
	start := time.Now()
	c, target, viaMesh, err := p.dialPeer(peer)
	p.mu.Lock()
	defer p.mu.Unlock()
	e.dialing--
	if err != nil {
		e.downAt, e.err, e.target = time.Now(), err.Error(), target
		return
	}
	e.downAt, e.err = time.Time{}, ""
	e.target, e.viaMesh = target, viaMesh
	e.sample(time.Since(start))
	if p.closed || p.peers[peer] != e {
		_ = c.Close()
		return
	}
	e.idle = append(e.idle, pooledConn{c: c, at: time.Now(), target: target, viaMesh: viaMesh})
}

func (p *pool) maintain() {
	t := time.NewTicker(5 * time.Second)
	defer t.Stop()
	for {
		select {
		case <-p.stop:
			return
		case <-t.C:
		}
		p.tidy()
	}
}

func (p *pool) tidy() {
	var refill []string
	p.mu.Lock()
	for id, e := range p.peers {
		keep := e.idle[:0]
		for _, x := range e.idle {
			if time.Since(x.at) < poolMaxAge && connAlive(x.c) {
				keep = append(keep, x)
			} else {
				_ = x.c.Close()
			}
		}
		clear(e.idle[len(keep):])
		e.idle = keep
		idle := time.Since(e.used)
		switch {
		case idle > poolIdle:
			for _, x := range e.idle {
				_ = x.c.Close()
			}
			delete(p.peers, id)
			continue
		case idle > 30*time.Second && e.want > poolMin:
			e.want = max(poolMin, e.want/2)
		}
		if len(e.idle) < e.want {
			refill = append(refill, id)
		}
	}
	p.mu.Unlock()
	for _, id := range refill {
		p.fill(id)
	}
}

// PeerPoolStatus describes one next-hop connection pool for diagnostics.
type PeerPoolStatus struct {
	Peer    string  `json:"peer"`
	Target  string  `json:"target,omitempty"`
	ViaMesh bool    `json:"viaMesh,omitempty"`
	Ready   int     `json:"ready"`
	RTTMs   float64 `json:"rttMs,omitempty"`
	Down    bool    `json:"down,omitempty"`
	Error   string  `json:"error,omitempty"`
}

func (p *pool) status() []PeerPoolStatus {
	p.mu.Lock()
	defer p.mu.Unlock()
	ids := make([]string, 0, len(p.peers))
	for id := range p.peers {
		ids = append(ids, id)
	}
	slices.Sort(ids)
	out := make([]PeerPoolStatus, 0, len(ids))
	for _, id := range ids {
		e := p.peers[id]
		target := e.target
		if target == "" {
			if t := p.targets[id]; t.direct != "" {
				target = t.direct
			} else {
				target = t.mesh
			}
		}
		var rtt float64
		if e.rtt > 0 {
			rtt = math.Round(float64(e.rtt)/float64(time.Microsecond)/100) / 10
		}
		out = append(out, PeerPoolStatus{
			Peer:    id,
			Target:  target,
			ViaMesh: e.viaMesh,
			Ready:   len(e.idle),
			RTTMs:   rtt,
			Down:    !e.downAt.IsZero() && time.Since(e.downAt) < peerDownHold,
			Error:   e.err,
		})
	}
	return out
}
