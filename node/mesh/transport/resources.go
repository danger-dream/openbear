package transport

import (
	"errors"
	"golang.zx2c4.com/wireguard/conn"
)

func (b *Bind) SetResourceProfile(profile string) error {
	if profile != "member" && profile != "relay" && profile != "" {
		return errors.New("invalid resource profile")
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	b.opt.ResourceProfile = profile
	return nil
}

func (b *Bind) ValidatePeerSet(peers []PeerIdentity) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	if len(peers) > b.opt.limits().paths {
		return ErrBackpressure
	}
	ids, keys := map[string]bool{}, map[[32]byte]bool{}
	for _, p := range peers {
		if p.ID == "" || len(p.ID) > 256 || ids[p.ID] || p.DataKey == ([32]byte{}) || p.ControlKey == ([32]byte{}) || p.DataKey == p.ControlKey || keys[p.DataKey] || keys[p.ControlKey] {
			return errors.New("invalid or duplicate peer identity")
		}
		ids[p.ID], keys[p.DataKey], keys[p.ControlKey] = true, true, true
	}
	return nil
}

func (b *Bind) DeviceLimits() conn.DeviceLimits {
	return b.deviceLimits(0)
}

func (p *PlaneBind) DeviceLimits() conn.DeviceLimits {
	return p.b.deviceLimits(1)
}

func (b *Bind) deviceLimits(plane uint8) conn.DeviceLimits {
	b.mu.Lock()
	defer b.mu.Unlock()
	l := conn.DeviceLimits{MaxPeers: b.opt.limits().paths, Workers: 2, BatchSize: 1, QueueDepth: 4, TxBuffers: 32, RxBuffers: 64, KeepaliveBuffers: 4}
	if plane == 1 {
		l.Workers, l.TxBuffers, l.RxBuffers = 1, 8, 16
	}
	if b.opt.ResourceProfile == "relay" {
		l.Workers *= 2
		l.TxBuffers *= 2
		l.RxBuffers *= 2
	}
	return l
}
