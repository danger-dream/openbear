package probe

import (
	"context"
	"fmt"
	"net"
	"net/netip"
	"sync/atomic"
	"testing"
	"time"

	"openbear/node/mesh/transport"
)

func TestConfigCheckValidatesTargetsAndLimits(t *testing.T) {
	var empty *Config
	if err := empty.Check(); err != nil {
		t.Fatal(err)
	}
	ok := &Config{
		IntervalS: 300,
		Targets: []Target{
			{Peer: "r1", Host: "10.0.0.1", UDP: 51999, TCP: 52000},
			{Peer: "r2", Host: "r2.example.com", TLS: 52001},
		},
	}
	if err := ok.Check(); err != nil {
		t.Fatal(err)
	}
	tooMany := &Config{Targets: make([]Target, MaxTargets+1)}
	for i := range tooMany.Targets {
		tooMany.Targets[i] = Target{Peer: fmt.Sprintf("r%d", i), Host: "10.0.0.1", UDP: 51999}
	}
	if err := tooMany.Check(); err == nil {
		t.Fatal("expected error for more than 64 targets")
	}
	dup := &Config{Targets: []Target{
		{Peer: "r1", Host: "10.0.0.1", UDP: 51999},
		{Peer: "r1", Host: "10.0.0.2", UDP: 51999},
	}}
	if err := dup.Check(); err == nil {
		t.Fatal("expected error for repeated peer")
	}
	noPort := &Config{Targets: []Target{{Peer: "r1", Host: "10.0.0.1"}}}
	if err := noPort.Check(); err == nil {
		t.Fatal("expected error for target without port")
	}
	badPort := &Config{Targets: []Target{{Peer: "r1", Host: "10.0.0.1", UDP: 70000}}}
	if err := badPort.Check(); err == nil {
		t.Fatal("expected error for invalid port")
	}
	badInterval := &Config{IntervalS: 7200}
	if err := badInterval.Check(); err == nil {
		t.Fatal("expected error for interval over 1h")
	}
}

func TestSummarizeComputesMedianRTTJitterAndLoss(t *testing.T) {
	at := time.UnixMilli(1700000000000)
	samples := []time.Duration{
		10 * time.Millisecond,
		12 * time.Millisecond,
		100 * time.Millisecond, // outlier
		11 * time.Millisecond,
	}
	res := Summarize("r1", MethodUDP, samples, 5, at)
	if res.Peer != "r1" || res.Method != MethodUDP || res.At != 1700000000000 {
		t.Fatalf("unexpected result header: %+v", res)
	}
	// Sorted: 10, 11, 12, 100 -> len 4, index 2 is 12.0 ms.
	if res.RTTMs == nil || *res.RTTMs != 12.0 {
		t.Fatalf("expected median RTT 12.0, got %v", res.RTTMs)
	}
	// Deviations from 12: |10-12|=2, |11-12|=1, |12-12|=0, |100-12|=88 -> sorted: 0, 1, 2, 88 -> index 2 is 2.0 ms.
	if res.JitterMs == nil || *res.JitterMs != 2.0 {
		t.Fatalf("expected median jitter 2.0, got %v", res.JitterMs)
	}
	if res.Loss != 0.2 {
		t.Fatalf("expected loss 0.2, got %v", res.Loss)
	}

	dead := Summarize("r2", MethodTCP, nil, 5, at)
	if dead.RTTMs != nil || dead.JitterMs != nil || dead.Loss != 1 {
		t.Fatalf("expected full loss without RTT/jitter, got %+v", dead)
	}
}

func TestMeasurePrefersUDPReflectorAndFallsBackToTCP(t *testing.T) {
	var udpCalls, tcpCalls int
	svc := &Service{
		Spacing: -1,
		Now:     func() time.Time { return time.UnixMilli(1700000001000) },
		Resolve: func(ctx context.Context, host string, timeout time.Duration) (netip.Addr, error) {
			return netip.MustParseAddr("127.0.0.1"), nil
		},
		ProbeUDP: func(ctx context.Context, addr netip.AddrPort, count int, timeout time.Duration) ([]time.Duration, string) {
			udpCalls++
			if addr.Port() == 51999 {
				return []time.Duration{20 * time.Millisecond, 21 * time.Millisecond, 19 * time.Millisecond, 20 * time.Millisecond, 22 * time.Millisecond}, transport.ProbeOK
			}
			return nil, transport.ProbeTimeout
		},
		ProbeTCP: func(ctx context.Context, addr string, timeout time.Duration) (time.Duration, string) {
			tcpCalls++
			return 35 * time.Millisecond, transport.ProbeOK
		},
	}

	got := svc.RunRound(context.Background(), []Target{
		{Peer: "udp-relay", Host: "127.0.0.1", UDP: 51999, TCP: 52000},
		{Peer: "tcp-only", Host: "127.0.0.1", TLS: 52001},
		{Peer: "udp-blocked", Host: "127.0.0.1", UDP: 51998, TCP: 52000},
	})
	if len(got) != 3 {
		t.Fatalf("expected 3 results, got %d", len(got))
	}
	if got[0].Method != MethodUDP || got[0].RTTMs == nil || *got[0].RTTMs != 20.0 || got[0].Loss != 0 {
		t.Fatalf("unexpected udp-relay result: %+v", got[0])
	}
	if got[1].Method != MethodTCP || got[1].RTTMs == nil || *got[1].RTTMs != 35.0 || got[1].Loss != 0 {
		t.Fatalf("unexpected tcp-only result: %+v", got[1])
	}
	if got[2].Method != MethodTCP || got[2].RTTMs == nil || *got[2].RTTMs != 35.0 {
		t.Fatalf("expected fallback to TCP when UDP times out, got %+v", got[2])
	}
	if udpCalls != 2 || tcpCalls != 10 {
		t.Fatalf("expected 2 UDP calls and 10 TCP calls, got %d and %d", udpCalls, tcpCalls)
	}
}

func TestConcurrencyAndCapLimits(t *testing.T) {
	var active, maxActive atomic.Int32
	svc := &Service{
		Spacing: time.Millisecond,
		Resolve: func(ctx context.Context, host string, timeout time.Duration) (netip.Addr, error) {
			return netip.MustParseAddr("127.0.0.1"), nil
		},
		ProbeUDP: func(ctx context.Context, addr netip.AddrPort, count int, timeout time.Duration) ([]time.Duration, string) {
			cur := active.Add(1)
			for {
				prev := maxActive.Load()
				if cur <= prev || maxActive.CompareAndSwap(prev, cur) {
					break
				}
			}
			time.Sleep(5 * time.Millisecond)
			active.Add(-1)
			return []time.Duration{10 * time.Millisecond}, transport.ProbeOK
		},
	}
	targets := make([]Target, 80)
	for i := range targets {
		targets[i] = Target{Peer: fmt.Sprintf("p%02d", i), Host: "127.0.0.1", UDP: 51999}
	}
	res := svc.RunRound(context.Background(), targets)
	if len(res) != MaxTargets {
		t.Fatalf("expected cap at %d targets, got %d", MaxTargets, len(res))
	}
	if m := maxActive.Load(); m > MaxParallel {
		t.Fatalf("concurrency %d exceeded MaxParallel %d", m, MaxParallel)
	}
}

func TestServiceApplyAndRealTCPProbe(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			c.Close()
		}
	}()
	port := ln.Addr().(*net.TCPAddr).Port

	svc := &Service{
		Spacing: -1,
		Jitter:  func(time.Duration) time.Duration { return 0 },
	}
	defer svc.Close()

	svc.Apply(&Config{
		IntervalS: 5,
		Targets:   []Target{{Peer: "local-tcp", Host: "127.0.0.1", TCP: port}},
	})
	deadline := time.Now().Add(2 * time.Second)
	var st []Result
	for time.Now().Before(deadline) {
		st = svc.Status()
		if len(st) == 1 && st[0].RTTMs != nil {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if len(st) != 1 || st[0].Peer != "local-tcp" || st[0].Method != MethodTCP || st[0].RTTMs == nil || st[0].Loss != 0 {
		t.Fatalf("expected live TCP probe result, got %+v", st)
	}
}
