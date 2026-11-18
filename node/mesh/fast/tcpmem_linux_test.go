//go:build linux

package fast

import (
	"fmt"
	"os"
	"path/filepath"
	"testing"
)

func TestCalcTCPMemWatermarksProtectionRaiseOnlyAndOrder(t *testing.T) {
	const page = 4096
	const gib = 1 << 30

	// 1. Standard 1 GiB RAM, 800 MiB available: 1/6, 1/5, 1/4.
	orig := [3]uint64{10000, 15000, 20000}
	got, wantLow := CalcTCPMem(orig, gib, 800<<20, page)
	if wantLow != (gib/6)/page {
		t.Fatalf("wantLow = %d", wantLow)
	}
	if got != [3]uint64{(gib / 6) / page, (gib / 5) / page, (gib / 4) / page} {
		t.Fatalf("1 GiB standard = %v", got)
	}

	// 2. Available memory protection: only 240 MiB available -> high = 120 MiB (< 1/6 = 170 MiB),
	//    low and pressure clamp down to high while staying >= orig, preserving monotonic order!
	gotCap, _ := CalcTCPMem(orig, gib, 240<<20, page)
	wantHigh := uint64((120 << 20) / page)
	if gotCap[2] != wantHigh || gotCap[0] > gotCap[1] || gotCap[1] > gotCap[2] {
		t.Fatalf("available memory cap broke order or cap: %v (wantHigh=%d)", gotCap, wantHigh)
	}

	// 3. Raise-only: when orig is already higher than calculated values, nothing is lowered.
	highOrig := [3]uint64{90000, 120000, 180000}
	gotKeep, _ := CalcTCPMem(highOrig, gib, 800<<20, page)
	if gotKeep != highOrig {
		t.Fatalf("lowered existing tcp_mem: got %v, want %v", gotKeep, highOrig)
	}
}

func TestTCPMemApplyRestoreCrashRecoveryAndReadOnlyContainer(t *testing.T) {
	dir := t.TempDir()
	proc := t.TempDir()
	tcpMemFile := filepath.Join(proc, "tcp_mem")
	memInfoFile := filepath.Join(proc, "meminfo")
	netstatFile := filepath.Join(proc, "netstat")
	bootIDFile := filepath.Join(proc, "boot_id")

	write := func(p, s string) {
		t.Helper()
		if err := os.WriteFile(p, []byte(s), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	write(tcpMemFile, "9500\t12600\t19000\n")
	write(memInfoFile, "MemTotal:        1048576 kB\nMemAvailable:     819200 kB\n")
	write(netstatFile, "TcpExt: SyncookiesSent TCPMemoryPressures\nTcpExt: 0 7\n")
	write(bootIDFile, "boot-1\n")

	newMgr := func() *tcpMemManager {
		return &tcpMemManager{
			dir:         dir,
			pageBytes:   4096,
			procTCPMem:  tcpMemFile,
			procMemInfo: memInfoFile,
			procNetstat: netstatFile,
			procBootID:  bootIDFile,
			cgroupPaths: []string{},
			containerFn: func() bool { return false },
		}
	}

	// 1. Apply raises tcp_mem and persists save file.
	m1 := newMgr()
	m1.init(dir)
	m1.apply()
	st := m1.status()
	if !st.Changed || st.Limited || st.Pressures != 7 || st.Original != [3]uint64{9500, 12600, 19000} {
		t.Fatalf("after apply: %+v", st)
	}
	if _, err := os.Stat(filepath.Join(dir, tcpMemSaveFile)); err != nil {
		t.Fatalf("save file missing: %v", err)
	}
	// The engine hands its hooks over again after falling back to an older
	// config: what this run raised stays raised and reported as such.
	raised, _ := readThreePages(tcpMemFile)
	m1.init(dir)
	m1.apply()
	if cur, _ := readThreePages(tcpMemFile); cur != raised || !m1.status().Changed {
		t.Fatalf("second init put back %v over the raised %v", cur, raised)
	}

	// 2. Simulate node crash (do NOT call m1.restore()): a new manager on the SAME boot_id
	//    restores the original 9500/12600/19000 on init()!
	m2 := newMgr()
	m2.init(dir)
	gotPages, err := readThreePages(tcpMemFile)
	if err != nil || gotPages != [3]uint64{9500, 12600, 19000} {
		t.Fatalf("crash recovery on same boot got %v (err=%v), want [9500 12600 19000]", gotPages, err)
	}

	// 3. Re-apply and clean restore() puts original back and removes save file.
	m2.apply()
	if cur, _ := readThreePages(tcpMemFile); cur[0] <= 9500 {
		t.Fatalf("re-apply did not raise: %v", cur)
	}
	m2.restore()
	if cur, _ := readThreePages(tcpMemFile); cur != [3]uint64{9500, 12600, 19000} {
		t.Fatalf("restore got %v", cur)
	}
	if _, err := os.Stat(filepath.Join(dir, tcpMemSaveFile)); !os.IsNotExist(err) {
		t.Fatalf("save file still exists after restore: %v", err)
	}

	// 4. Stale save file from an earlier boot_id is discarded without clobbering new kernel defaults.
	_ = os.WriteFile(filepath.Join(dir, tcpMemSaveFile), []byte(`{"original":[1000,2000,3000],"applied":[4000,5000,6000],"bootId":"old-boot"}`), 0o600)
	write(tcpMemFile, "11000\t14000\t21000\n")
	m3 := newMgr()
	m3.init(dir)
	if cur, _ := readThreePages(tcpMemFile); cur != [3]uint64{11000, 14000, 21000} {
		t.Fatalf("reboot clobbered fresh kernel tcp_mem: %v", cur)
	}

	// 5. Read-only / missing tcp_mem in container sets ReadOnly=true, Limited=true and does not fail.
	mContainer := &tcpMemManager{
		dir:         t.TempDir(),
		pageBytes:   4096,
		procTCPMem:  filepath.Join(proc, "nonexistent_in_container_ns", "tcp_mem"),
		procMemInfo: memInfoFile,
		procNetstat: netstatFile,
		procBootID:  bootIDFile,
		cgroupPaths: []string{},
		containerFn: func() bool { return true },
	}
	mContainer.init(mContainer.dir)
	mContainer.apply()
	cst := mContainer.status()
	if !cst.Container || !cst.ReadOnly || !cst.Limited {
		t.Fatalf("expected container readOnly+limited diagnostic, got %+v", fmt.Sprintf("%+v", cst))
	}
}
