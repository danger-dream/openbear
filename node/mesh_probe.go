//go:build linux || darwin || windows

package node

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net"
	"net/netip"
	"slices"
	"strconv"
	"sync"
	"time"

	"openbear/node/mesh/transport"
)

// mesh.probe: before this server joins, what it measures to each relay of
// the mesh, from the node itself (no mesh service needed): the round trip and
// loss of UDP reflector requests, or of TCP connects when UDP does not answer,
// and whether each transport the relay listens on answers. A port of 0 is not
// listened on and is not tried.
const (
	maxMeshProbeTargets   = 64
	maxMeshProbeCount     = 10
	meshProbeParallel     = 16
	defaultMeshProbeCount = 4
)

type meshProbeTarget struct {
	ID   string   `json:"id"`
	Host string   `json:"host"`
	Pins []string `json:"pins"`
	UDP  int      `json:"udp"`
	TCP  int      `json:"tcp"`
	TLS  int      `json:"tls"`
}

type meshProbeResult struct {
	ID    string   `json:"id"`
	RTTMs *float64 `json:"rttMs"`
	Loss  *float64 `json:"loss"`
	UDP   string   `json:"udp"`
	TCP   string   `json:"tcp"`
	TLS   string   `json:"tls"`
}

func meshProbe(ctx context.Context, raw json.RawMessage) (any, error) {
	var p struct {
		Targets   []meshProbeTarget `json:"targets"`
		TimeoutMs int           `json:"timeoutMs"`
		Count     int           `json:"count"`
	}
	d := json.NewDecoder(bytes.NewReader(raw))
	d.DisallowUnknownFields()
	if d.Decode(&p) != nil || len(p.Targets) > maxMeshProbeTargets || p.Count < 0 || p.Count > maxMeshProbeCount || p.TimeoutMs < 0 || p.TimeoutMs > 10000 {
		return nil, errors.New("invalid_params")
	}
	for _, t := range p.Targets {
		if t.ID == "" || t.Host == "" || !meshProbePort(t.UDP) || !meshProbePort(t.TCP) || !meshProbePort(t.TLS) {
			return nil, errors.New("invalid_params")
		}
	}
	count, timeout := p.Count, time.Duration(p.TimeoutMs)*time.Millisecond
	if count == 0 {
		count = defaultMeshProbeCount
	}
	if timeout == 0 {
		timeout = 2 * time.Second
	}
	results := make([]meshProbeResult, len(p.Targets))
	slots := make(chan struct{}, meshProbeParallel)
	var wg sync.WaitGroup
	for i, t := range p.Targets {
		wg.Add(1)
		go func() {
			defer wg.Done()
			slots <- struct{}{}
			defer func() { <-slots }()
			results[i] = meshProbeOne(ctx, t, count, timeout)
		}()
	}
	wg.Wait()
	return map[string]any{"results": results}, nil
}

func meshProbePort(p int) bool { return p >= 0 && p <= 65535 }

func meshProbeOne(ctx context.Context, t meshProbeTarget, count int, timeout time.Duration) meshProbeResult {
	out := meshProbeResult{ID: t.ID, UDP: "closed", TCP: "closed", TLS: "closed"}
	addr, err := meshProbeResolve(ctx, t.Host, timeout)
	if err != nil {
		for _, s := range []*string{&out.UDP, &out.TCP, &out.TLS} {
			*s = transport.ProbeTimeout
		}
		return out
	}
	var wg sync.WaitGroup
	var udp, tcp []time.Duration
	if t.UDP > 0 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			udp, out.UDP = transport.ProbeUDP(ctx, netip.AddrPortFrom(addr, uint16(t.UDP)), count, timeout)
		}()
	}
	if t.TCP > 0 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			took, state := transport.ProbeTCP(ctx, meshProbeAddr(addr, t.TCP), timeout)
			if out.TCP = state; state == transport.ProbeOK {
				tcp = append(tcp, took)
			}
		}()
	}
	if t.TLS > 0 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			out.TLS = transport.ProbeTLS(ctx, meshProbeAddr(addr, t.TLS), t.Pins, timeout)
		}()
	}
	wg.Wait()
	// UDP answers give the round trip; without them, a few more TCP connects do.
	sent := count
	rtts := udp
	if len(udp) == 0 && len(tcp) > 0 {
		for i := 1; i < count && ctx.Err() == nil; i++ {
			if took, state := transport.ProbeTCP(ctx, meshProbeAddr(addr, t.TCP), timeout); state == transport.ProbeOK {
				tcp = append(tcp, took)
			}
		}
		rtts = tcp
	} else if len(udp) == 0 {
		return out
	}
	slices.Sort(rtts)
	ms := float64(rtts[len(rtts)/2].Microseconds()) / 1000
	loss := float64(sent-len(rtts)) / float64(sent)
	out.RTTMs, out.Loss = &ms, &loss
	return out
}

func meshProbeResolve(ctx context.Context, host string, timeout time.Duration) (netip.Addr, error) {
	if a, err := netip.ParseAddr(host); err == nil {
		return a.Unmap(), nil
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	addrs, err := net.DefaultResolver.LookupNetIP(ctx, "ip", host)
	if err != nil || len(addrs) == 0 {
		return netip.Addr{}, errors.New("unresolved")
	}
	// IPv4 first: the panel's addresses, and what the relay reports, are.
	for _, a := range addrs {
		if a.Unmap().Is4() {
			return a.Unmap(), nil
		}
	}
	return addrs[0], nil
}

func meshProbeAddr(a netip.Addr, port int) string { return net.JoinHostPort(a.String(), strconv.Itoa(port)) }
