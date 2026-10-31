//go:build linux

package node

import (
	"fmt"
	"os"
	"path/filepath"
	"syscall"
	"testing"
)

func write(t *testing.T, root, name, content string) {
	t.Helper()
	p := filepath.Join(root, name)
	os.MkdirAll(filepath.Dir(p), 0o755)
	if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestCollectorReadsProcFixtures(t *testing.T) {
	root := t.TempDir()
	write(t, root, "proc/stat", "cpu  100 0 100 800 0 0 0 0 0 0\nbtime 1700000000\n")
	write(t, root, "proc/meminfo", "MemTotal:       1000 kB\nMemFree:  100 kB\nMemAvailable:    400 kB\nSwapTotal: 200 kB\nSwapFree: 50 kB\n")
	write(t, root, "proc/loadavg", "0.50 0.25 0.10 1/100 1234\n")
	write(t, root, "proc/uptime", "3600.50 1000.00\n")
	write(t, root, "proc/net/dev", "Inter-|\n face |\n    lo: 999 0 0 0 0 0 0 0 999 0 0 0 0 0 0 0\n  eth0: 1000 0 0 0 0 0 0 0 2000 0 0 0 0 0 0 0\ndocker0: 50 0 0 0 0 0 0 0 50 0 0 0 0 0 0 0\n   wg0: 70 0 0 0 0 0 0 0 70 0 0 0 0 0 0 0\n")
	write(t, root, "proc/net/sockstat", "sockets: used 290\nTCP: inuse 7 orphan 0 tw 3 alloc 9 mem 1\nUDP: inuse 4 mem 2\nUDPLITE: inuse 0\n")
	write(t, root, "proc/net/sockstat6", "TCP6: inuse 2\nUDP6: inuse 1\nUDPLITE6: inuse 0\n")
	write(t, root, "proc/diskstats", "   8       0 sda 10 0 1000 0 20 0 2000 0 0 0 0\n   8       1 sda1 10 0 1000 0 20 0 2000 0 0 0 0\n   7       0 loop0 5 0 500 0 0 0 0 0 0 0 0\n 253       0 dm-0 5 0 900 0 5 0 1900 0 0 0 0\n")
	write(t, root, "sys/block/sda/device/vendor", "QEMU\n")
	write(t, root, "sys/block/loop0/size", "0\n")
	write(t, root, "sys/block/dm-0/size", "0\n")
	write(t, root, "proc/self/mountinfo", "22 1 254:1 / / rw - ext4 /dev/vda1 rw\n23 22 254:1 /x /bind rw - ext4 /dev/vda1 rw\n24 22 0:5 / /proc rw - proc proc rw\n")
	write(t, root, "proc/self/statm", "2000 1500 300 100 0 400 0\n")
	write(t, root, "proc/42/status", "")
	write(t, root, "etc/os-release", "PRETTY_NAME=\"Debian GNU/Linux 12 (bookworm)\"\n")
	write(t, root, "proc/cpuinfo", "processor : 0\nmodel name\t: AMD EPYC 7B13\n")
	os.MkdirAll(filepath.Join(root, "bind"), 0o755)
	var st syscall.Statfs_t
	syscall.Statfs(root, &st)
	c := &Collector{root: root}
	c.Sample()
	// 1000 jiffies: 300 busy (incl. 100 steal), 100 iowait, 600 idle.
	write(t, root, "proc/stat", "cpu  250 0 150 1400 100 0 0 100 0 0\nbtime 1700000000\n")
	write(t, root, "proc/net/dev", "  eth0: 4000 0 0 0 0 0 0 0 2500 0 0 0 0 0 0 0\n")
	write(t, root, "proc/diskstats", "   8       0 sda 10 0 3000 0 20 0 6000 0 0 0 0\n   8       1 sda1 10 0 3000 0 20 0 6000 0 0 0 0\n 253       0 dm-0 5 0 9000 0 5 0 9000 0 0 0 0\n")
	s := c.Sample()
	if s.CPU != 30 || s.IOWait != 10 || s.Steal != 10 || s.Mem.Total != 1024000 || s.Mem.Used != 614400 || s.Swap.Used != 153600 {
		t.Fatalf("cpu/mem: %+v", s)
	}
	if s.Net.RX != 4000 || s.Net.TX != 2500 || s.Net.RXRate == 0 {
		t.Fatalf("net counters must skip lo/docker/wg: %+v", s.Net)
	}
	// Only the whole disk counts: 2000 sectors read and 4000 written since the first sample.
	if diff := int64(s.IO.WriteRate) - 2*int64(s.IO.ReadRate); s.IO.ReadRate == 0 || diff < -2 || diff > 2 {
		t.Fatalf("disk io must count sda only: %+v", s.IO)
	}
	if s.Load[0] != 0.5 || s.Uptime != 3600 || s.Boot != 1700000000 || s.TCP != 12 || s.UDP != 5 || s.Processes != 1 {
		t.Fatalf("misc: %+v", s)
	}
	if s.NodeRSS != 1500*int64(os.Getpagesize()) {
		t.Fatalf("rss: %d", s.NodeRSS)
	}
	// The bind mount of the same device is counted once.
	if s.Disk.Total != int64(st.Blocks)*int64(st.Bsize) || s.Disk.Used > s.Disk.Total {
		t.Fatalf("disk: %+v", s.Disk)
	}
	info := c.Info()
	if info.OS != "Debian GNU/Linux 12 (bookworm)" || info.CPU != "AMD EPYC 7B13" || info.DiskTotal != s.Disk.Total {
		t.Fatalf("info: %+v", info)
	}
}

// From the 10-30 check: the mesh interface, a bridge over eth0, a bond and a VLAN were
// counted on top of the interfaces below them, doubling the traffic.
func TestTrafficCountsNoLayeredInterfaces(t *testing.T) {
	line := func(name string, n int) string {
		return fmt.Sprintf("%s: %d 1 0 0 0 0 0 0 %d 1 0 0 0 0 0 0\n", name, n, n)
	}
	root := t.TempDir()
	write(t, root, "proc/net/dev", "Inter-|\n face |\n"+line("eth0", 1000)+line("eth1", 500)+line("obmesh0", 900)+
		line("br0", 1000)+line("bond0", 1500)+line("eth0.100", 300)+line("venet0", 70))
	for name, kind := range map[string]string{"br0": "bridge", "bond0": "bond", "eth0.100": "vlan"} {
		write(t, root, "sys/class/net/"+name+"/uevent", "DEVTYPE="+kind+"\nINTERFACE="+name+"\n")
	}
	write(t, root, "sys/class/net/eth0/uevent", "INTERFACE=eth0\n")
	if rx, tx := (&Collector{root: root}).network(); rx != 1570 || tx != 1570 {
		t.Fatalf("rx=%d tx=%d, want eth0+eth1+venet0", rx, tx)
	}
}
