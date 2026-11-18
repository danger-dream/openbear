//go:build linux

package fast

import (
	"net/netip"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestReviewTCPMemFailuresKeepRecoveryEvidence(t *testing.T) {
	dir, proc := t.TempDir(), t.TempDir()
	path := filepath.Join(proc, "tcp_mem")
	write := func(p, s string) {
		t.Helper()
		if e := os.WriteFile(p, []byte(s), 0600); e != nil {
			t.Fatal(e)
		}
	}
	write(path, "1000 2000 3000\n")
	info := filepath.Join(proc, "meminfo")
	write(info, "MemTotal: 1048576 kB\nMemAvailable: 819200 kB\n")
	boot := filepath.Join(proc, "boot")
	write(boot, "same-boot")
	mgr := func() *tcpMemManager {
		return &tcpMemManager{dir: dir, pageBytes: 4096, procTCPMem: path, procMemInfo: info, procBootID: boot, cgroupPaths: []string{}, containerFn: func() bool { return false }}
	}
	m := mgr()
	m.init(dir)
	// Saving into a file-as-directory must not change the simulated sysctl.
	bad := filepath.Join(dir, "not-a-directory")
	write(bad, "x")
	m.dir = bad
	m.apply()
	if cur, _ := readThreePages(path); cur != ([3]uint64{1000, 2000, 3000}) || m.changed || m.err == "" {
		t.Fatal("modified without a recovery record")
	}
	m.dir = dir
	m.apply()
	if !m.changed {
		t.Fatalf("retry failed: %s", m.err)
	}
	saved := filepath.Join(dir, tcpMemSaveFile)
	// A directory in place of sysctl reliably fails even as root.
	if e := os.Remove(path); e != nil {
		t.Fatal(e)
	}
	if e := os.Mkdir(path, 0700); e != nil {
		t.Fatal(e)
	}
	m.restore()
	if !m.changed || !m.restorePending || m.err == "" {
		t.Fatal("failed restore claimed success")
	}
	if _, e := os.Stat(saved); e != nil {
		t.Fatal("failed restore discarded original", e)
	}
	// Restart on the same boot must likewise keep evidence and pending state.
	next := mgr()
	next.init(dir)
	if next.inited || !next.changed {
		t.Fatal("failed startup restoration marked complete")
	}
	if _, e := os.Stat(saved); e != nil {
		t.Fatal("startup discarded recovery", e)
	}
	if e := os.Remove(path); e != nil {
		t.Fatal(e)
	}
	write(path, "4000 5000 6000\n")
	next.restore()
	if cur, _ := readThreePages(path); cur != ([3]uint64{1000, 2000, 3000}) || next.changed || next.err != "" {
		t.Fatalf("retry restoration: %v %s", cur, next.err)
	}
	if _, e := os.Stat(saved); !os.IsNotExist(e) {
		t.Fatal("successful restore kept record", e)
	}
}

func TestReviewCaptureSerializesUpdateHealAndFinalClose(t *testing.T) {
	old := transparentCmd
	defer func() { transparentCmd = old }()
	entered, release := make(chan struct{}), make(chan struct{})
	var once sync.Once
	var mu sync.Mutex
	var operations []string
	transparentCmd = func(stdin string, args ...string) (string, error) {
		line := strings.Join(args, " ")
		if strings.Contains(stdin, "add element") {
			once.Do(func() { close(entered); <-release })
		}
		mu.Lock()
		operations = append(operations, line+"\n"+stdin)
		mu.Unlock()
		return "", nil
	}
	cr := &captureRunner{active: true, portLocal: 12345}
	first, second, closed := make(chan struct{}), make(chan struct{}), make(chan struct{})
	go func() { cr.update([]targetKey{{netip.MustParseAddr("100.96.0.2"), 443, protoTCP}}); close(first) }()
	<-entered
	go func() { cr.update(nil); close(second) }()
	go func() { cr.close(); close(closed) }()
	close(release)
	for _, ch := range []chan struct{}{first, second, closed} {
		select {
		case <-ch:
		case <-time.After(time.Second):
			t.Fatal("kernel operation serialization stuck")
		}
	}
	mu.Lock()
	n := len(operations)
	mu.Unlock()
	cr.heal()
	cr.update([]targetKey{{netip.MustParseAddr("100.96.0.2"), 443, protoTCP}})
	mu.Lock()
	defer mu.Unlock()
	if len(operations) != n {
		t.Fatal("old runner recreated rules after close")
	}
	lastDelete := -1
	for i, op := range operations {
		if strings.HasPrefix(op, "nft delete table") {
			lastDelete = i
		}
		if strings.Contains(op, "route flush") || strings.Contains(op, "route replace") {
			t.Fatal("touched unowned routes")
		}
	}
	if lastDelete < 0 {
		t.Fatal("missing final cleanup")
	}
	for _, op := range operations[lastDelete+1:] {
		if strings.HasPrefix(op, "nft -f") {
			t.Fatal("nft update completed after cleanup")
		}
	}
}

func TestReviewHealRemovesUnexpectedSetElements(t *testing.T) {
	old := transparentCmd
	defer func() { transparentCmd = old }()
	var scripts []string
	transparentCmd = func(stdin string, args ...string) (string, error) {
		if strings.Join(args, " ") == "nft list table ip "+Table {
			return "set tcp_ready { elements = { 100.96.0.2 . 443 } } redirect to :12345", nil
		}
		if stdin != "" {
			scripts = append(scripts, stdin)
		}
		return "", nil
	}
	cr := &captureRunner{active: true, portLocal: 12345}
	cr.heal()
	if len(scripts) != 1 || scripts[0] != NFTSets(nil, nil) {
		t.Fatalf("did not reconcile empty desired sets: %v", scripts)
	}
}
