//go:build linux && !race

package transport

import (
	"os"
	"runtime"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func memoryKiB(t *testing.T, name string) uint64 {
	t.Helper()
	raw, err := os.ReadFile("/proc/self/status")
	if err != nil {
		t.Fatal(err)
	}
	for _, line := range strings.Split(string(raw), "\n") {
		fields := strings.Fields(line)
		if len(fields) > 1 && fields[0] == name+":" {
			v, err := strconv.ParseUint(fields[1], 10, 64)
			if err != nil {
				t.Fatal(err)
			}
			return v
		}
	}
	t.Fatal("missing " + name)
	return 0
}
func TestTwoPlaneMemoryBudget(t *testing.T) {
	for _, profile := range []string{"member", "relay"} {
		t.Run(profile, func(t *testing.T) {
			l := newPathLab(t)
			if profile == "relay" {
				opt := l.z.opt
				opt.ResourceProfile = "relay"
				if err := l.z.Reconfigure(opt); err != nil {
					t.Fatal(err)
				}
			}
			active, remote := l.prepare(t, l.tcp)
			l.commit(t, active, remote)
			l.prepare(t, l.tls)
			stop := make(chan struct{})
			finished := make(chan struct{})
			var received atomic.Uint64
			go func() {
				defer close(finished)
				for {
					select {
					case <-stop:
						return
					case p := <-l.zd.tun.Inbound:
						if len(p) == 1400 {
							received.Add(1)
						}
					}
				}
			}()
			defer func() { close(stop); <-finished }()
			packet := dataPacket(l.ad, l.zd, make([]byte, 1380))
			start := time.Now()
			for i := 0; i < 12000; i++ {
				l.ad.tun.Outbound <- packet
				if i%64 == 0 {
					time.Sleep(time.Millisecond)
				}
			}
			waitUntil(t, 5*time.Second, "business load received", func() bool { return received.Load() > 10000 })
			sendControl(t, l.ac, l.zc, l.a, active, "post-load-control", 900)
			runtime.GC()
			var m runtime.MemStats
			runtime.ReadMemStats(&m)
			rss, peak := memoryKiB(t, "VmRSS"), memoryKiB(t, "VmHWM")
			limit := uint64(160 * 1024)
			if profile == "relay" {
				limit = 256 * 1024
			}
			if peak > limit {
				t.Fatalf("%s process high-water RSS %d KiB > %d KiB", profile, peak, limit)
			}
			t.Logf("%s: 2 nodes/4 WG instances incl. control, 2 physical paths; sent=12000 received=%d duration=%s heap=%d RSS=%d KiB peak=%d KiB sockets=%+v/%+v", profile, received.Load(), time.Since(start), m.HeapAlloc, rss, peak, l.a.Resources(), l.z.Resources())
		})
	}
}
