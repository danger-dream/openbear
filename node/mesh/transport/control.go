package transport

import (
	"context"
	"net"
	"time"

	"golang.zx2c4.com/wireguard/conn"
)

type controlRun struct {
	ctx    context.Context
	cancel context.CancelFunc
	rx     chan received
}
type PlaneBind struct {
	b   *Bind
	run *controlRun
}

func (b *Bind) ControlBind() *PlaneBind { return &PlaneBind{b: b} }
func (p *PlaneBind) Open(port uint16) ([]conn.ReceiveFunc, uint16, error) {
	b := p.b
	b.mu.Lock()
	defer b.mu.Unlock()
	r := b.run
	if r == nil {
		return nil, 0, net.ErrClosed
	}
	if r.control != nil {
		return nil, 0, conn.ErrBindAlreadyOpen
	}
	ctx, cancel := context.WithCancel(r.ctx)
	c := &controlRun{ctx: ctx, cancel: cancel, rx: make(chan received, 64)}
	r.control = c
	p.run = c
	return []conn.ReceiveFunc{func(bufs [][]byte, sizes []int, eps []conn.Endpoint) (int, error) {
		return b.receiveFrom(r, c.ctx, c.rx, bufs, sizes, eps)
	}}, 0, nil
}
func (p *PlaneBind) Close() error {
	b := p.b
	b.mu.Lock()
	r := b.run
	if r == nil || p.run == nil || r.control != p.run {
		b.mu.Unlock()
		return nil
	}
	c := r.control
	r.control = nil
	p.run = nil
	c.cancel()
	b.mu.Unlock()
	for {
		select {
		case v := <-c.rx:
			b.releasePacket(r, v)
		default:
			return nil
		}
	}
}
func (p *PlaneBind) BatchSize() int                                   { return 1 }
func (p *PlaneBind) SetMark(mark uint32) error                        { return p.b.SetMark(mark) }
func (p *PlaneBind) ParseEndpoint(s string) (conn.Endpoint, error)    { return p.b.ParseEndpoint(s) }
func (p *PlaneBind) AuthenticatedReceive(m conn.ReceiveMetadata) bool { return p.b.authenticated(1, m) }
func (p *PlaneBind) Send(bufs [][]byte, e conn.Endpoint) error        { return p.b.send(bufs, e, 1, false) }
func (p *PlaneBind) SendWithMetadata(bufs [][]byte, key [32]byte, m conn.SendMetadata) error {
	return p.b.sendMetadata(bufs, key, m, 1)
}
func (b *Bind) SendWithMetadata(bufs [][]byte, key [32]byte, m conn.SendMetadata) error {
	return b.sendMetadata(bufs, key, m, 0)
}
func (b *Bind) sendMetadata(bufs [][]byte, key [32]byte, m conn.SendMetadata, plane uint8) error {
	if e, ok := m.Endpoint.(*punchEndpoint); ok && e.b == b {
		return b.sendPunch(bufs, key, e, plane)
	}
	b.mu.Lock()
	ep, err := b.resolve(m.Endpoint, plane, true)
	if err == nil && b.managed {
		p := b.peers[ep.peer]
		if p == nil {
			err = ErrSecurity
		} else {
			want := p.identity.DataKey
			if plane == 1 {
				want = p.identity.ControlKey
			}
			if key != want {
				err = ErrSecurity
			}
		}
	}
	var info PathInfo
	if err == nil {
		info = b.info(ep, plane, time.Time{})
	}
	b.mu.Unlock()
	timing := sendTiming{}
	if err == nil {
		err = b.sendMeasured(bufs, m.Endpoint, plane, true, &timing)
	}
	if m.ID != 0 {
		b.mu.Lock()
		b.event(PathEvent{Kind: "sent", Path: info, SendID: m.ID, WriteStartedAt: timing.start, WriteCompletedAt: timing.end, Err: err})
		b.mu.Unlock()
	}
	return err
}
func (b *Bind) resolve(target conn.Endpoint, plane uint8, explicit bool) (*endpoint, error) {
	var ep *endpoint
	switch e := target.(type) {
	case *endpoint:
		if e.b != b {
			return nil, conn.ErrWrongEndpointType
		}
		ep = e
	case *pathEndpoint:
		if e.b != b {
			return nil, conn.ErrWrongEndpointType
		}
		if e.generation != e.ref.Generation || e.retired {
			return nil, net.ErrClosed
		}
		ep = e.endpoint
	case *logicalEndpoint:
		if e.b != b {
			return nil, conn.ErrWrongEndpointType
		}
		p := b.peers[e.peer]
		if p == nil {
			return nil, net.ErrClosed
		}
		ref := p.active
		if plane == 1 && ref.ID == "" {
			ref = p.bootstrap
		}
		if ep := b.ids[ref.ID]; ep == nil || ep.retired {
			return nil, ErrNoPath
		}
		return b.path(ref)
	default:
		return nil, conn.ErrWrongEndpointType
	}
	if ep.peer != "" && !explicit && plane == 0 {
		p := b.peers[ep.peer]
		if p == nil {
			return nil, net.ErrClosed
		}
		return b.path(p.active)
	}
	return ep, nil
}
