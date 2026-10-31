package health

import (
	"sync"
	"testing"
	"time"
)

func TestProposalFailureAndTimeoutChooseBackup(t *testing.T) {
	for _, failure := range []string{"path", "timeout", "generation"} {
		t.Run(failure, func(t *testing.T) {
			m, b, now := policyManager(t)
			m.active = ""
			m.choose(now)
			old := *m.proposal
			p := m.candidates[old.candidate]
			switch failure {
			case "path":
				m.failed(p, now.Add(time.Second))
			case "timeout":
				now = old.expires
			case "generation":
				p.path.Generation++
				p.ready = false
			}
			m.choose(now.Add(time.Second))
			if m.proposal == nil || m.proposal.candidate != "backup" || m.proposal.sequence <= old.sequence {
				t.Fatalf("did not move past failed proposal: %+v", m.proposal)
			}
			m.finishCommit(Received{Peer: p.config.Peer, Path: old.path}, message{Sequence: old.sequence, Nonce: old.nonce, Token: old.token, Generation: old.path.Generation}, now.Add(time.Second))
			if b.commits != 0 {
				t.Fatal("stale confirmation committed")
			}
			q := m.proposal
			backup := m.candidates[q.candidate]
			m.finishCommit(Received{Peer: backup.config.Peer, Path: q.path}, message{Sequence: q.sequence, Nonce: q.nonce, Token: q.token, Generation: q.path.Generation}, now.Add(2*time.Second))
			if m.active != "backup" || b.commits != 1 || m.proposal != nil {
				t.Fatal("backup did not commit")
			}
		})
	}
}

func TestPendingReturnDoesNotBlockActiveFailure(t *testing.T) {
	m, _, now := policyManager(t)
	p := m.candidates["preferred"]
	seq, _ := m.store.nextSequence()
	m.proposal = newProposal(p, seq, "return", false, now)
	spare := *m.candidates["backup"]
	spare.config = Candidate{ID: "third", Peer: "C"}
	spare.path = Path{"third", 1}
	spare.track = newTracker(m.cfg.Parameters, now)
	m.candidates["third"] = &spare
	m.cfg.Candidates = append(m.cfg.Candidates, spare.config)
	m.candidates["backup"].track.stats.Failures = m.cfg.Parameters.ConsecutiveFailures
	m.choose(now.Add(time.Second))
	if m.proposal == nil || m.proposal.candidate != "third" || m.proposal.sequence <= seq {
		t.Fatalf("stalled return blocked failover: %+v", m.proposal)
	}
}

func TestConcurrentSnapshotInitializesRestoredTrendOnce(t *testing.T) {
	m, _, _ := policyManager(t)
	p := m.candidates["preferred"]
	p.config.Endpoint = "tcp://192.0.2.1:1234"
	m.redial = DefaultRedialParams()
	m.baselines = map[string]savedTrend{"preferred": {Endpoint: p.config.Endpoint, Buckets: []savedBucket{{At: time.Now().Unix(), MinUs: 1000, Periods: 30}}}}
	var wg sync.WaitGroup
	start := make(chan struct{})
	for range 64 {
		wg.Add(1)
		go func() { defer wg.Done(); <-start; m.Snapshot() }()
	}
	close(start)
	wg.Wait()
	if p.trend == nil || len(p.trend.buckets) != 1 || !p.trend.saved {
		t.Fatal("restored trend lost")
	}
}

func TestActivePunchedEntryRediscovered(t *testing.T) {
	for _, change := range []bool{false, true} {
		t.Run(map[bool]string{false: "probe failure", true: "new configured endpoint"}[change], func(t *testing.T) {
			m, b := offerManager(t, Config{Network: "n", Self: "M", Role: "member", Revision: 1, Mode: "fixed", Candidates: []Candidate{{ID: "c1", Peer: "R", Punch: true, Endpoint: "udp://192.0.2.1:51998"}}, Nodes: []Node{{ID: "R", Role: "relay"}}}, nil, map[string]bool{"R": true})
			now := time.Now()
			p := m.candidates["c1"]
			p.config.Endpoint = "udp://198.51.100.1:40000"
			p.path = Path{"old", 1}
			p.ready = true
			p.routeReady = true
			p.since = now.Add(-time.Minute)
			p.track.stats.Failures = m.cfg.Parameters.ConsecutiveFailures
			m.active = "c1"
			m.discovered = true
			m.reflecting = true
			d := m.directs["R"]
			d.state = directUp
			if change {
				cfg := m.cfg
				cfg.Revision++
				cfg.Candidates = append([]Candidate(nil), cfg.Candidates...)
				cfg.Candidates[0].Endpoint = "udp://192.0.2.99:51998"
				if err := m.configure(cfg); err != nil {
					t.Fatal(err)
				}
			}
			m.tick(now.Add(time.Second))
			m.tick(p.retry.Add(time.Second))
			if p.config.Endpoint != "" || p.path.ID != "" || d.state == directUp || m.active != "" || len(b.punched) == 0 {
				t.Fatalf("no rediscovery: active=%s endpoint=%s path=%v state=%s punches=%d", m.active, p.config.Endpoint, p.path, d.state, len(b.punched))
			}
		})
	}
}

func TestPunchedConfigChangeKeepsHealthyPath(t *testing.T) {
	m, _ := offerManager(t, Config{Network: "n", Self: "M", Role: "member", Revision: 1, Mode: "fixed", Candidates: []Candidate{{ID: "c", Peer: "R", Punch: true}}, Nodes: []Node{{ID: "R", Role: "relay"}}}, nil, nil)
	p := m.candidates["c"]
	p.path = Path{"healthy", 1}
	p.ready = true
	p.routeReady = true
	p.config.Endpoint = "udp://198.51.100.1:40000"
	m.active = "c"
	cfg := m.cfg
	cfg.Revision++
	cfg.Candidates = append([]Candidate(nil), cfg.Candidates...)
	cfg.Candidates[0].Endpoint = "udp://192.0.2.99:51998"
	if err := m.configure(cfg); err != nil {
		t.Fatal(err)
	}
	if m.active != "c" || p.path.ID != "healthy" || p.config.Endpoint != "udp://198.51.100.1:40000" {
		t.Fatal("healthy path broken")
	}
}
