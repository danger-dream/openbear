//go:build linux

package fast

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
)

const tcpMemSaveFile = "fast_tcp_mem.json"

type savedTCPMem struct {
	Original [3]uint64 `json:"original"`
	Applied  [3]uint64 `json:"applied"`
	BootID   string    `json:"bootId,omitempty"`
}

type tcpMemManager struct {
	mu          sync.Mutex
	dir         string
	pageBytes   int
	procTCPMem  string
	procMemInfo string
	procNetstat string
	procBootID  string
	cgroupPaths []string
	containerFn func() bool

	restorePending bool
	inited         bool
	hasOrig        bool
	orig           [3]uint64
	cur            [3]uint64
	changed        bool
	limited        bool
	readOnly       bool
	err            string
}

func (m *tcpMemManager) defaultsLocked() {
	if m.pageBytes <= 0 {
		m.pageBytes = os.Getpagesize()
	}
	if m.procTCPMem == "" {
		m.procTCPMem = "/proc/sys/net/ipv4/tcp_mem"
	}
	if m.procMemInfo == "" {
		m.procMemInfo = "/proc/meminfo"
	}
	if m.procNetstat == "" {
		m.procNetstat = "/proc/net/netstat"
	}
	if m.procBootID == "" {
		m.procBootID = "/proc/sys/kernel/random/boot_id"
	}
	if m.cgroupPaths == nil {
		m.cgroupPaths = []string{"/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"}
	}
	if m.containerFn == nil {
		m.containerFn = detectContainer
	}
}

// init checks whether a previous node run crashed without restoring tcp_mem
// on this boot and restores the saved original value first. It does so once:
// the engine hands its hooks over again after falling back to an older config,
// while what this run itself raised is still in force.
func (m *tcpMemManager) init(dir string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.inited {
		return
	}
	m.dir = dir
	m.defaultsLocked()
	m.inited = m.recoverSavedLocked()
}

func (m *tcpMemManager) statePathLocked() string {
	if m.dir == "" {
		return ""
	}
	return filepath.Join(m.dir, tcpMemSaveFile)
}

func (m *tcpMemManager) bootIDLocked() string {
	raw, err := os.ReadFile(m.procBootID)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(raw))
}

func (m *tcpMemManager) failure(err error) bool {
	m.err, m.readOnly, m.limited = err.Error(), true, true
	return false
}

func (m *tcpMemManager) recoverSavedLocked() bool {
	path := m.statePathLocked()
	if path == "" {
		return true
	}
	raw, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return true
	}
	if err != nil {
		return m.failure(err)
	}
	var saved savedTCPMem
	if err := json.Unmarshal(raw, &saved); err != nil {
		return m.failure(err)
	}
	if saved.Original == ([3]uint64{}) {
		return m.failure(fmt.Errorf("invalid tcp_mem recovery record"))
	}
	boot := m.bootIDLocked()
	if saved.BootID != "" && boot != "" && saved.BootID != boot {
		if err := os.Remove(path); err != nil {
			return m.failure(err)
		}
		return true
	}
	m.orig, m.hasOrig, m.changed, m.restorePending = saved.Original, true, true, true
	return m.restoreLocked()
}

// Saving must finish durably before any kernel write. Never truncate the only
// recovery record: a crash or disk-full leaves either the old or new whole file.
func (m *tcpMemManager) saveLocked(target [3]uint64) error {
	path := m.statePathLocked()
	if path == "" {
		return fmt.Errorf("no tcp_mem recovery directory")
	}
	if err := os.MkdirAll(m.dir, 0700); err != nil {
		return err
	}
	data, err := json.Marshal(savedTCPMem{Original: m.orig, Applied: target, BootID: m.bootIDLocked()})
	if err != nil {
		return err
	}
	f, err := os.CreateTemp(m.dir, ".fast_tcp_mem-*")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	if _, err = f.Write(data); err == nil {
		err = f.Sync()
	}
	cerr := f.Close()
	if err != nil {
		return err
	}
	if cerr != nil {
		return cerr
	}
	if err = os.Rename(f.Name(), path); err != nil {
		return err
	}
	dir, err := os.Open(m.dir)
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}

func (m *tcpMemManager) apply() {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.defaultsLocked()
	if !m.inited {
		m.inited = m.recoverSavedLocked()
		if !m.inited {
			return
		}
	}
	if m.restorePending && !m.restoreLocked() {
		return
	}
	if m.changed {
		if cur, err := readThreePages(m.procTCPMem); err == nil {
			m.cur = cur
		}
		return
	}

	total, avail := m.readMemoryLocked()
	cur, err := readThreePages(m.procTCPMem)
	if err != nil {
		m.readOnly = true
		m.limited = true
		m.err = err.Error()
		return
	}
	if !m.hasOrig {
		m.orig = cur
		m.hasOrig = true
	}
	m.cur = cur

	target, wantLow := CalcTCPMem(m.orig, total, avail, uint64(m.pageBytes))
	if target == cur {
		m.readOnly = false
		m.limited = false
		m.err = ""
		return
	}

	// Persist original values before modifying kernel sysctl so a crash can recover.
	if err := m.saveLocked(target); err != nil {
		m.failure(err)
		return
	}
	// Even a failed write may be partial; retain the record until restoration.
	m.changed = true
	if err := writeThreePages(m.procTCPMem, target); err != nil {
		m.restorePending = true
		m.failure(err)
		m.limited = cur[0] < wantLow || cur[2] < target[2]
		return
	}

	m.cur = target
	m.changed = true
	m.readOnly = false
	m.limited = false
	m.err = ""
}

func (m *tcpMemManager) restore() {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.defaultsLocked()
	if !m.inited {
		m.inited = m.recoverSavedLocked()
		return
	}
	m.restoreLocked()
}

func (m *tcpMemManager) restoreLocked() bool {
	if m.changed && m.hasOrig {
		m.restorePending = true
		if err := writeThreePages(m.procTCPMem, m.orig); err != nil {
			return m.failure(err)
		}
		m.cur = m.orig
	}
	if path := m.statePathLocked(); path != "" {
		if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
			return m.failure(err)
		}
	}
	m.changed, m.restorePending, m.readOnly = false, false, false
	m.err = ""
	return true
}

func (m *tcpMemManager) status() *TCPMemStatus {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.defaultsLocked()
	if cur, err := readThreePages(m.procTCPMem); err == nil {
		m.cur = cur
		if !m.hasOrig {
			m.orig = cur
			m.hasOrig = true
		}
	}
	pb := uint64(m.pageBytes)
	return &TCPMemStatus{
		PageBytes:     m.pageBytes,
		Original:      m.orig,
		Current:       m.cur,
		OriginalBytes: [3]uint64{m.orig[0] * pb, m.orig[1] * pb, m.orig[2] * pb},
		CurrentBytes:  [3]uint64{m.cur[0] * pb, m.cur[1] * pb, m.cur[2] * pb},
		Changed:       m.changed,
		Limited:       m.limited,
		ReadOnly:      m.readOnly,
		Container:     m.containerFn(),
		Pressures:     readTCPPressures(m.procNetstat),
		Error:         m.err,
	}
}

// SweepTCPMem restores any saved tcp_mem left behind in dir after an unclean stop.
func SweepTCPMem(dir string) {
	var m tcpMemManager
	m.init(dir)
}

func (m *tcpMemManager) readMemoryLocked() (total, avail uint64) {
	raw, err := os.ReadFile(m.procMemInfo)
	if err != nil {
		return 0, 0
	}
	var memFree, buffers, cached uint64
	for _, line := range strings.Split(string(raw), "\n") {
		f := strings.Fields(line)
		if len(f) < 2 {
			continue
		}
		v, err := strconv.ParseUint(f[1], 10, 64)
		if err != nil {
			continue
		}
		switch f[0] {
		case "MemTotal:":
			total = v << 10
		case "MemAvailable:":
			avail = v << 10
		case "MemFree:":
			memFree = v << 10
		case "Buffers:":
			buffers = v << 10
		case "Cached:":
			cached = v << 10
		}
	}
	if avail == 0 {
		avail = memFree + buffers + cached
	}
	for _, p := range m.cgroupPaths {
		if craw, err := os.ReadFile(p); err == nil {
			if n, err := strconv.ParseUint(strings.TrimSpace(string(craw)), 10, 64); err == nil && n > 0 && (total == 0 || n < total) {
				total = n
				if avail > total {
					avail = total
				}
			}
		}
	}
	return total, avail
}

func readThreePages(path string) ([3]uint64, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return [3]uint64{}, err
	}
	f := strings.Fields(string(raw))
	if len(f) < 3 {
		return [3]uint64{}, fmt.Errorf("unexpected tcp_mem %q", string(raw))
	}
	var out [3]uint64
	for i := range 3 {
		v, err := strconv.ParseUint(f[i], 10, 64)
		if err != nil {
			return [3]uint64{}, err
		}
		out[i] = v
	}
	return out, nil
}

func writeThreePages(path string, v [3]uint64) error {
	s := fmt.Sprintf("%d\t%d\t%d\n", v[0], v[1], v[2])
	return os.WriteFile(path, []byte(s), 0o644)
}

func readTCPPressures(path string) uint64 {
	raw, err := os.ReadFile(path)
	if err != nil {
		return 0
	}
	var headers []string
	for _, line := range strings.Split(string(raw), "\n") {
		if !strings.HasPrefix(line, "TcpExt:") {
			continue
		}
		f := strings.Fields(line)
		if headers == nil {
			headers = f
			continue
		}
		for i := 1; i < len(headers) && i < len(f); i++ {
			if headers[i] == "TCPMemoryPressures" {
				v, _ := strconv.ParseUint(f[i], 10, 64)
				return v
			}
		}
		headers = nil
	}
	return 0
}

func detectContainer() bool {
	for _, p := range []string{"/.dockerenv", "/run/.containerenv"} {
		if _, err := os.Stat(p); err == nil {
			return true
		}
	}
	if raw, err := os.ReadFile("/proc/1/environ"); err == nil {
		s := string(raw)
		if strings.Contains(s, "container=lxc") || strings.Contains(s, "container=docker") || strings.Contains(s, "container=podman") || strings.Contains(s, "container=systemd-nspawn") {
			return true
		}
	}
	if raw, err := os.ReadFile("/proc/1/cgroup"); err == nil {
		s := string(raw)
		if strings.Contains(s, "/lxc/") || strings.Contains(s, "/docker/") || strings.Contains(s, "/kubepods") || strings.Contains(s, "libpod-") {
			return true
		}
	}
	return false
}
