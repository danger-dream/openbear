//go:build darwin

package node

import (
	"context"
	"testing"
	"time"
)

func TestCollectorReportsThisMac(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	for range 4 {
		go func() {
			for ctx.Err() == nil {
			}
		}()
	}
	c := NewCollector()
	c.Sample()
	deadline := time.Now().Add(15 * time.Second)
	s := c.Sample()
	for s.CPU == 0 && time.Now().Before(deadline) {
		time.Sleep(time.Second)
		s = c.Sample()
	}
	if s.CPU <= 5 || s.CPU > 100 {
		t.Fatalf("cpu with four busy goroutines: %v", s.CPU)
	}
	info := c.Info()
	if info.Platform != "darwin" || info.OS == "macOS" || info.CPU == "" || info.MemTotal <= 0 || len(info.MachineID) < 16 {
		t.Fatalf("info: %+v", info)
	}
	if s.Mem.Used <= 0 || s.Mem.Used >= s.Mem.Total || s.Disk.Used <= 0 || s.Disk.Used >= s.Disk.Total || info.DiskTotal != s.Disk.Total {
		t.Fatalf("mem/disk: %+v %+v", s.Mem, s.Disk)
	}
	if s.Net.RX == 0 || s.Load[0] <= 0 || s.Processes < 50 || s.Boot <= 0 || s.Uptime <= 0 || s.NodeRSS <= 1<<20 {
		t.Fatalf("net/load/misc: %+v", s)
	}
}
