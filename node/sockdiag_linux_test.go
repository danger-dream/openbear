//go:build linux

package node

import (
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestTCPStates(t *testing.T) {
	root := t.TempDir()
	os.MkdirAll(filepath.Join(root, "proc/net"), 0o755)
	os.WriteFile(filepath.Join(root, "proc/net/snmp"), []byte("Ip: Forwarding DefaultTTL\nIp: 1 64\n"+
		"Tcp: RtoAlgorithm RtoMin RtoMax MaxConn ActiveOpens PassiveOpens AttemptFails EstabResets CurrEstab InSegs\n"+
		"Tcp: 1 200 120000 -1 10 20 1 2 37 999\n"), 0o644)
	c := &Collector{root: root, listening: func() (int, bool) { return 5, true }}
	estab, listen := c.tcpStates(time.Now())
	if estab == nil || *estab != 37 || listen == nil || *listen != 5 {
		t.Fatalf("estab %v listen %v", estab, listen)
	}
	// Without the files or the query: nothing claimed.
	c = &Collector{root: t.TempDir()}
	if estab, listen := c.tcpStates(time.Now()); estab != nil || listen != nil {
		t.Fatalf("made up: %v %v", estab, listen)
	}
}

func TestProcListeningCountsListenRows(t *testing.T) {
	root := t.TempDir()
	head := "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
	os.WriteFile(filepath.Join(root, "tcp"), []byte(head+
		"   0: 00000000:0016 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 1\n"+
		"   1: FE0A0A0A:0016 640A0A0A:D1F2 01 00000000:00000000 00:00000000 00000000     0        0 2\n"), 0o644)
	os.WriteFile(filepath.Join(root, "tcp6"), []byte(head+
		"   0: 00000000000000000000000000000000:0035 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 3\n"), 0o644)
	if n, ok := procListening(root); !ok || n != 2 {
		t.Fatalf("%d %v", n, ok)
	}
	if _, ok := procListening(t.TempDir()); ok {
		t.Fatal("counted without tables")
	}
}

func TestTCPListeningCountsAListener(t *testing.T) {
	before, ok := tcpListening()
	if !ok {
		t.Skip("no socket diag here")
	}
	l, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	after, ok := tcpListening()
	if !ok || after < before+1 {
		t.Fatalf("before %d after %d", before, after)
	}
}
