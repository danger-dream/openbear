//go:build linux

package fast

import (
	"os"
	"path/filepath"
	"testing"
)

// isolateHost points s's tcp_mem tuning at files of its own, so a test run as
// root never changes this machine's kernel settings. Call before SetHooks.
func isolateHost(t *testing.T, s *Service) {
	t.Helper()
	dir := t.TempDir()
	write := func(name, body string) string {
		p := filepath.Join(dir, name)
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
		return p
	}
	s.tcpmem.procTCPMem = write("tcp_mem", "1000\t2000\t3000\n")
	s.tcpmem.procMemInfo = write("meminfo", "MemTotal:        1048576 kB\nMemAvailable:     524288 kB\n")
	s.tcpmem.procNetstat = filepath.Join(dir, "netstat")
	s.tcpmem.procBootID = write("boot_id", "test-boot\n")
	s.tcpmem.cgroupPaths = []string{}
	s.tcpmem.containerFn = func() bool { return false }
}
