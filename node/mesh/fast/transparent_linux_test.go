//go:build linux

package fast

import (
	"errors"
	"net/netip"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeSystem stands in for nft and ip: it records every command and fails
// UDP's routing rule (no TPROXY) and, while broken is set, every nft load.
// While noNFT is set, nft is not installed.
type fakeSystem struct {
	mu      sync.Mutex
	calls   []string
	scripts []string
	broken  bool
	noNFT   bool
}

func (f *fakeSystem) install(t *testing.T) {
	transparentCmd, transparentLook = f.run, f.look
	t.Cleanup(func() { transparentCmd, transparentLook = runSystemCmd, lookSystemBin })
}

func (f *fakeSystem) look(name string) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if name == "nft" && f.noNFT {
		return "", errors.New("nft not found")
	}
	return "/usr/sbin/" + name, nil
}

func (f *fakeSystem) run(stdin string, args ...string) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	line := strings.Join(args, " ")
	f.calls = append(f.calls, line)
	if line == "nft -f -" {
		f.scripts = append(f.scripts, stdin)
	}
	switch {
	case strings.HasPrefix(line, "ip -4 rule add"):
		return "", errors.New("ip: Operation not supported")
	case line == "nft -f -" && f.broken:
		return "", errors.New("nft: Could not process rule")
	}
	return "", nil
}

func (f *fakeSystem) count(line string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	n := 0
	for _, c := range f.calls {
		if c == line {
			n++
		}
	}
	return n
}

func (f *fakeSystem) setBroken(v bool) {
	f.mu.Lock()
	f.broken = v
	f.mu.Unlock()
}

func (f *fakeSystem) setNoNFT(v bool) {
	f.mu.Lock()
	f.noNFT = v
	f.mu.Unlock()
}

func (f *fakeSystem) lastScript() string {
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.scripts) == 0 {
		return ""
	}
	return f.scripts[len(f.scripts)-1]
}

func (f *fakeSystem) total() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.calls)
}

func transparentConfig(t *testing.T, self netip.Addr) *Config {
	return &Config{
		MeshPort: 52003,
		Keys:     []string{randKeyB64(t)},
		Nodes:    map[string]netip.Addr{"A": self, "C": netip.MustParseAddr("100.96.0.3")},
		Targets:  []Target{{Node: "C", Port: 48888, Proto: "any"}},
		Capture:  true,
	}
}

// Without UDP TPROXY the takeover runs on TCP alone, and the periodic retry
// leaves it be; when its rules did not load at all, the retry loads them.
func TestTransparentRetryOnlyWhenRulesAreMissing(t *testing.T) {
	sys := &fakeSystem{}
	sys.install(t)
	self := netip.MustParseAddr("100.96.0.1")
	mesh := netip.MustParsePrefix("100.96.0.0/16")

	var tcpOnly Service
	defer tcpOnly.Close()
	isolateHost(t, &tcpOnly)
	tcpOnly.SetHooks(Hooks{Dir: t.TempDir()})
	tcpOnly.Apply(transparentConfig(t, self), "A", self, mesh, nil)
	st := tcpOnly.Status().Capture
	if st == nil || !st.Active || st.UDP || !strings.Contains(st.Error, "UDP TPROXY unavailable") {
		t.Fatalf("TCP-only takeover status = %+v", st)
	}
	runner, loads, deletes := tcpOnly.capture, sys.count("nft -f -"), sys.count("nft delete table ip "+Table)
	for range 3 {
		tcpOnly.Retry()
	}
	if tcpOnly.capture != runner || sys.count("nft -f -") != loads || sys.count("nft delete table ip "+Table) != deletes {
		t.Fatalf("retry rebuilt a working TCP-only takeover: loads %d -> %d, deletes %d -> %d",
			loads, sys.count("nft -f -"), deletes, sys.count("nft delete table ip "+Table))
	}

	sys.setBroken(true)
	var failed Service
	defer failed.Close()
	isolateHost(t, &failed)
	failed.SetHooks(Hooks{Dir: t.TempDir()})
	failed.Apply(transparentConfig(t, self), "A", self, mesh, nil)
	if st := failed.Status().Capture; st == nil || st.Active {
		t.Fatalf("takeover whose rules failed to load reports %+v", st)
	}
	sys.setBroken(false)
	failed.Retry()
	if st := failed.Status().Capture; st == nil || !st.Active {
		t.Fatalf("retry did not load the missing rules: %+v", st)
	}
}

// Without nft the takeover opens nothing and the periodic retry leaves it be
// until nft is installed; then the next retry starts it.
func TestTransparentWithoutNFTWaitsForIt(t *testing.T) {
	sys := &fakeSystem{noNFT: true}
	sys.install(t)
	self := netip.MustParseAddr("100.96.0.1")
	mesh := netip.MustParsePrefix("100.96.0.0/16")

	var s Service
	defer s.Close()
	isolateHost(t, &s)
	s.SetHooks(Hooks{Dir: t.TempDir()})
	s.Apply(transparentConfig(t, self), "A", self, mesh, nil)
	st := s.Status().Capture
	if st == nil || st.Active || st.Missing != "nft" {
		t.Fatalf("takeover without nft reports %+v", st)
	}
	runner := s.capture
	if runner.lnLocalTCP != nil || runner.lnLANTCP != nil || runner.udpConn != nil {
		t.Fatal("takeover without nft opened its listeners")
	}
	calls := sys.total()
	for range 3 {
		s.Retry()
	}
	if s.capture != runner || sys.total() != calls {
		t.Fatalf("retry without nft rebuilt the takeover or ran %d commands", sys.total()-calls)
	}

	sys.setNoNFT(false)
	s.Retry()
	if st := s.Status().Capture; st == nil || !st.Active || st.Missing != "" {
		t.Fatalf("retry after nft was installed reports %+v", st)
	}
}

// Capture takes a fast port only while its channel is ready: the ready sets
// follow the channels, without reloading the table.
func TestCaptureFollowsReadyChannels(t *testing.T) {
	sys := &fakeSystem{}
	sys.install(t)
	self := netip.MustParseAddr("100.96.0.1")
	mesh := netip.MustParsePrefix("100.96.0.0/16")

	var s Service
	defer s.Close()
	isolateHost(t, &s)
	s.SetHooks(Hooks{Dir: t.TempDir()})
	s.Apply(transparentConfig(t, self), "A", self, mesh, nil)
	if !strings.Contains(sys.lastScript(), "set tcp_ready {\n\t\ttype ipv4_addr . inet_service\n\t}") {
		t.Fatalf("capture began with ports taken:\n%s", sys.lastScript())
	}
	deletes := sys.count("nft delete table ip " + Table)

	chans := s.live.Load()
	chans.mu.Lock()
	now := time.Now()
	chans.byDest["C"] = &channel{dest: "C", opened: now, lastUse: now, refreshed: now.Add(time.Hour),
		cands: []*candidate{{plan: Plan{Hops: []string{"C"}, Planned: true}, state: pathReady, since: now, next: now.Add(time.Hour)}}}
	chans.mu.Unlock()
	s.readyChanged()
	if want := NFTSets([]string{"100.96.0.3 . 48888"}, []string{"100.96.0.3 . 48888"}); sys.lastScript() != want {
		t.Fatalf("ready channel: %q, want %q", sys.lastScript(), want)
	}

	chans.failed("C", Plan{Hops: []string{"C"}, Planned: true}, errors.New("gone"))
	if want := NFTSets(nil, nil); sys.lastScript() != want {
		t.Fatalf("channel down: %q, want %q", sys.lastScript(), want)
	}
	if sys.count("nft delete table ip "+Table) != deletes {
		t.Fatal("the ready sets reloaded the table")
	}
}
