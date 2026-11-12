package proxy

import (
	"context"
	"errors"
	"net"
	"net/netip"
	"sync"
	"time"
)

// Tunnel pool: an entry keeps connections to each exit it serves open
// before it has clients for them, so a new client costs no handshake across
// the mesh. Each tunnel carries one client and is never reused.
const (
	poolMin    = 2
	poolMax    = 16
	poolMaxAge = 45 * time.Second // the exit gives up waiting at headerWait
	poolIdle   = 5 * time.Minute  // an exit nobody used this long keeps none, unless warmed
	tunnelDial = 3 * time.Second
	exitDown   = 10 * time.Second // after a failed dial an exit is tried last
)

type pooled struct {
	c  *net.TCPConn
	at time.Time
}

type exitPool struct {
	idle    []pooled
	want    int
	used    time.Time
	kept    bool // a user's first exit: never let go
	dialing int
	downAt  time.Time
	err     string
	// rtt: how long a tunnel takes to open, smoothed; a round trip across
	// the mesh to the exit. Zero until the first one opens.
	rtt time.Duration
}

type pool struct {
	port   int
	mu     sync.Mutex
	exits  map[netip.Addr]*exitPool
	closed bool
	stop   chan struct{}
	// dial, when set, replaces dialing exit:port: tests.
	dial func(ctx context.Context, exit netip.Addr) (*net.TCPConn, error)
}

func newPool(port int) *pool {
	p := &pool{port: port, exits: map[netip.Addr]*exitPool{}, stop: make(chan struct{})}
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
	close(p.stop)
	for _, e := range p.exits {
		for _, x := range e.idle {
			x.c.Close()
		}
		e.idle = nil
	}
}

func (p *pool) exit(a netip.Addr) *exitPool {
	e := p.exits[a]
	if e == nil {
		e = &exitPool{want: poolMin}
		p.exits[a] = e
	}
	return e
}

// sample folds the time a tunnel took to open into the exit's round trip;
// called with p.mu held.
func (e *exitPool) sample(took time.Duration) {
	if e.rtt == 0 {
		e.rtt = took
	} else {
		e.rtt = (e.rtt*7 + took*3) / 10
	}
}

// latency is a's smoothed round trip, if a tunnel to it has opened.
func (p *pool) latency(a netip.Addr) (time.Duration, bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	e := p.exits[a]
	if e == nil || e.rtt == 0 {
		return 0, false
	}
	return e.rtt, true
}

func (p *pool) dialExit(ctx context.Context, a netip.Addr) (*net.TCPConn, error) {
	if p.dial != nil {
		return p.dial(ctx, a)
	}
	d := net.Dialer{Timeout: tunnelDial, KeepAlive: 15 * time.Second}
	c, err := d.DialContext(ctx, "tcp", netip.AddrPortFrom(a, uint16(p.port)).String())
	if err != nil {
		return nil, err
	}
	tc := c.(*net.TCPConn)
	tc.SetNoDelay(true)
	return tc, nil
}

// warm keeps tunnels ready to each of exits whether or not they are used,
// and lets every other exit go once nobody uses it.
func (p *pool) warm(exits []netip.Addr) {
	p.mu.Lock()
	for _, e := range p.exits {
		e.kept = false
	}
	for _, a := range exits {
		p.exit(a).kept = true
	}
	p.mu.Unlock()
	for _, a := range exits {
		p.fill(a)
	}
}

// down is whether a failed to dial lately.
func (p *pool) down(a netip.Addr) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	e := p.exits[a]
	return e != nil && !e.downAt.IsZero() && time.Since(e.downAt) < exitDown
}

// get gives a tunnel to a: a ready one that is still open, else one dialed
// now. Taking the last ready one asks for more next time.
func (p *pool) get(a netip.Addr) (*net.TCPConn, error) {
	p.mu.Lock()
	e := p.exit(a)
	e.used = time.Now()
	var c *net.TCPConn
	for len(e.idle) > 0 && c == nil {
		x := e.idle[len(e.idle)-1]
		e.idle = e.idle[:len(e.idle)-1]
		if time.Since(x.at) < poolMaxAge && alive(x.c) {
			c = x.c
		} else {
			x.c.Close()
		}
	}
	if len(e.idle) == 0 {
		e.want = min(e.want*2, poolMax)
	}
	p.mu.Unlock()
	p.fill(a)
	if c != nil {
		return c, nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), tunnelDial)
	defer cancel()
	start := time.Now()
	c, err := p.dialExit(ctx, a)
	p.mu.Lock()
	if err != nil {
		e.downAt, e.err = time.Now(), err.Error()
	} else {
		e.downAt, e.err = time.Time{}, ""
		e.sample(time.Since(start))
	}
	p.mu.Unlock()
	return c, err
}

// alive: a waiting tunnel has nothing to read until its header goes out, so
// a read that times out at once finds it open, and EOF or a reset finds it
// gone.
func alive(c *net.TCPConn) bool {
	c.SetReadDeadline(time.Now())
	var b [1]byte
	_, err := c.Read(b[:])
	c.SetReadDeadline(time.Time{})
	var ne net.Error
	return errors.As(err, &ne) && ne.Timeout()
}

// fill dials, all at once, the tunnels a lacks of those it wants ready, so a
// burst of clients finds them again one round trip later.
func (p *pool) fill(a netip.Addr) {
	p.mu.Lock()
	defer p.mu.Unlock()
	e := p.exit(a)
	if p.closed || (!e.downAt.IsZero() && time.Since(e.downAt) < exitDown) {
		return
	}
	for n := e.want - len(e.idle) - e.dialing; n > 0; n-- {
		e.dialing++
		go p.add(a, e)
	}
}

func (p *pool) add(a netip.Addr, e *exitPool) {
	ctx, cancel := context.WithTimeout(context.Background(), tunnelDial)
	start := time.Now()
	c, err := p.dialExit(ctx, a)
	cancel()
	p.mu.Lock()
	defer p.mu.Unlock()
	e.dialing--
	if err != nil {
		e.downAt, e.err = time.Now(), err.Error()
		return
	}
	e.downAt, e.err = time.Time{}, ""
	e.sample(time.Since(start))
	if p.closed || p.exits[a] != e {
		c.Close()
		return
	}
	e.idle = append(e.idle, pooled{c: c, at: time.Now()})
}

// maintain replaces tunnels before the exit drops them, shrinks what an
// exit keeps as it goes quiet, and lets go of exits nobody uses.
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
	var refill []netip.Addr
	p.mu.Lock()
	for a, e := range p.exits {
		keep := e.idle[:0]
		for _, x := range e.idle {
			if time.Since(x.at) < poolMaxAge-10*time.Second {
				keep = append(keep, x)
			} else {
				x.c.Close()
			}
		}
		clear(e.idle[len(keep):])
		e.idle = keep
		idle := time.Since(e.used)
		switch {
		case idle > poolIdle && !e.kept:
			for _, x := range e.idle {
				x.c.Close()
			}
			delete(p.exits, a)
			continue
		case idle > 30*time.Second && e.want > poolMin:
			e.want = max(poolMin, e.want/2)
		}
		if len(e.idle) < e.want {
			refill = append(refill, a)
		}
	}
	p.mu.Unlock()
	for _, a := range refill {
		p.fill(a)
	}
}

type poolStatus struct {
	Ready int           `json:"ready"`
	Down  bool          `json:"down,omitempty"`
	Error string        `json:"error,omitempty"`
	RTT   time.Duration `json:"-"`
}

func (p *pool) status(a netip.Addr) poolStatus {
	p.mu.Lock()
	defer p.mu.Unlock()
	e := p.exits[a]
	if e == nil {
		return poolStatus{}
	}
	return poolStatus{Ready: len(e.idle), Down: !e.downAt.IsZero() && time.Since(e.downAt) < exitDown, Error: e.err, RTT: e.rtt}
}
