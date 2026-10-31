package transport

import (
	"crypto/tls"
	"errors"
)

func (b *Bind) Reconfigure(opt Options) error {
	opt, err := options(opt)
	if err != nil {
		return err
	}
	b.op.Lock()
	defer b.op.Unlock()
	var cert *tls.Certificate
	var info Certificate
	if opt.ListenTLS != 0 {
		cert, info, err = activeCertificate(opt.TLSDir)
		if err != nil {
			return err
		}
	}
	b.mu.Lock()
	r := b.run
	old := b.opt
	if r == nil {
		b.opt = opt
		b.mu.Unlock()
		return nil
	}
	oldUDP := r.udp
	needUDP := opt.ListenUDP != 0 || opt.UDP
	for _, ep := range b.paths {
		needUDP = needUDP || (ep.outbound && ep.transport == "udp")
	}
	oldListeners := map[string]*listener{}
	for t, l := range r.listeners {
		oldListeners[t] = l
	}
	b.mu.Unlock()
	added := map[string]*listener{}
	var nextUDP *udpSocket
	changeUDP := old.ListenUDP != opt.ListenUDP || (needUDP && oldUDP == nil)
	abort := func(e error) error {
		for _, l := range added {
			l.Close()
		}
		if nextUDP != nil {
			nextUDP.Close()
		}
		return e
	}
	if changeUDP && needUDP {
		nextUDP, err = openUDP(opt.ListenUDP)
		if err != nil {
			return abort(err)
		}
	}
	for _, t := range []string{"tcp", "tls"} {
		p := opt.ListenTCP
		if t == "tls" {
			p = opt.ListenTLS
		}
		if p == 0 || (oldListeners[t] != nil && oldListeners[t].port == p) {
			continue
		}
		l, err := newListener(t, p, opt.TLSDir)
		if err != nil {
			return abort(err)
		}
		added[t] = l
	}
	b.mu.Lock()
	lim := opt.limits()
	cost := r.socketBytes
	if changeUDP {
		if oldUDP != nil {
			cost -= oldUDP.buffers
		}
		if nextUDP != nil {
			cost += nextUDP.buffers
		}
	}
	if len(b.paths) > lim.paths || len(r.sessions) > lim.paths || r.unauth > lim.unauth || cost > lim.socketBytes || r.queuedBytes > lim.rxBytes {
		b.mu.Unlock()
		return abort(errors.New("new resource profile cannot fit active paths"))
	}
	b.opt = opt
	var removed []*listener
	for t, l := range r.listeners {
		p := opt.ListenTCP
		if t == "tls" {
			p = opt.ListenTLS
		}
		if p == 0 || p != l.port {
			delete(r.listeners, t)
			l.Close()
			removed = append(removed, l)
		} else if t == "tls" {
			l.cert = cert
			l.certificateID = info.ID
		}
	}
	var closing []*session
	for s := range r.sessions {
		for _, l := range removed {
			if s.listener == l {
				s.conn.Close()
				closing = append(closing, s)
			}
		}
	}
	for t, l := range added {
		r.listeners[t] = l
		r.wg.Add(1)
		go b.accept(r, l)
	}
	if changeUDP {
		if oldUDP != nil {
			oldUDP.Close()
			r.socketBytes -= oldUDP.buffers
		}
		r.udp = nil
		if nextUDP != nil {
			b.startUDP(r, nextUDP)
		}
		for key, ep := range b.paths {
			if ep.transport == "udp" {
				if !ep.outbound {
					ep.retired = true
					delete(b.ids, ep.id)
					delete(b.paths, key)
				} else if nextUDP != nil {
					b.newGeneration(ep)
					ep.stats.LocalAddr = nextUDP.local
				}
			}
		}
	}
	b.mu.Unlock()
	for _, l := range removed {
		<-l.done
	}
	for _, s := range closing {
		<-s.done
	}
	if changeUDP && oldUDP != nil {
		<-oldUDP.done
	}
	return nil
}
