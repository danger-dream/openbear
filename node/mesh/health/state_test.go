package health

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestProbeDeadlinesAndLocalPressure(t *testing.T) {
	now := time.Now()
	p := Defaults()
	tr := newTracker(p, now)
	for i := uint64(1); i <= 3; i++ {
		at := now.Add(time.Duration(i-1) * time.Second)
		if !tr.queue(i, at) {
			t.Fatal("queue")
		}
		tr.written(i, at, at, nil)
	}
	tr.tick(now.Add(3 * time.Second))
	if tr.stats.Failures != 1 {
		t.Fatal(tr.stats)
	}
	tr.tick(now.Add(5 * time.Second))
	if tr.stats.Failures != 3 {
		t.Fatal(tr.stats)
	}
	if tr.reply(1, now.Add(6*time.Second)) || tr.stats.Late != 1 || tr.stats.SRTT != 0 {
		t.Fatal("late reply changed health", tr.stats)
	}
	tr.queue(4, now.Add(6*time.Second))
	tr.tick(now.Add(10 * time.Second))
	if tr.stats.LocalExpired != 1 || tr.stats.Failures != 3 {
		t.Fatal("local queue counted as network loss", tr.stats)
	}
	tr.queue(5, now.Add(11*time.Second))
	tr.written(5, now.Add(12*time.Second), now.Add(12*time.Second), nil)
	if !tr.reply(5, now.Add(12150*time.Millisecond)) || tr.stats.RTT != 150*time.Millisecond || tr.stats.Queue != time.Second {
		t.Fatal(tr.stats)
	}
	if tr.timeout() != 2*time.Second {
		t.Fatal(tr.timeout())
	}
}

func TestProbeOutstandingBound(t *testing.T) {
	now := time.Now()
	tr := newTracker(Defaults(), now)
	for i := uint64(0); i < 16; i++ {
		if !tr.queue(i, now) {
			t.Fatal(i)
		}
	}
	if tr.queue(17, now) {
		t.Fatal("unbounded probes")
	}
}

func TestOwnerReplayConflictRevocationAndRestart(t *testing.T) {
	dir := t.TempDir()
	s, err := openStore(dir, "n", "B")
	if err != nil {
		t.Fatal(err)
	}
	o := newOwners(s, 30*time.Second)
	allowed := map[string][]string{"M": {"A", "B"}}
	relays := map[string]bool{"A": true, "B": true, "C": true}
	if err = configureOwners(o, 1, allowed, relays); err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	n := sn(Notice{Member: "M", Owner: "A", Seq: 3, Refresh: 1, Generation: 1, LeaseMs: 30000})
	if changed, err := o.accept("A", sn(n), now); !changed || err != nil {
		t.Fatal(changed, err)
	}
	if _, err = o.accept("A", sn(n), now.Add(20*time.Second)); !errors.Is(err, errReplay) {
		t.Fatal(err)
	}
	if _, ok := o.owner("M", now.Add(31*time.Second)); ok {
		t.Fatal("replay extended lease")
	}
	n.Refresh++
	o.accept("A", sn(n), now.Add(32*time.Second))
	n.Owner = "B"
	n.Refresh++
	if _, err = o.accept("B", sn(n), now.Add(33*time.Second)); !errors.Is(err, errConflict) {
		t.Fatal(err)
	}
	if owner, ok := o.owner("M", now.Add(34*time.Second)); !ok || owner != "A" {
		t.Fatal("conflict lost last good claim")
	}
	n.Owner = "A"
	if _, err = o.accept("A", sn(n), now.Add(35*time.Second)); !errors.Is(err, errConflict) {
		t.Fatal("conflict must not renew", err)
	}
	n.Owner = "B"
	n.Seq++
	if _, err = o.accept("B", sn(n), now.Add(36*time.Second)); err != nil {
		t.Fatal(err)
	}
	s2, err := openStore(dir, "n", "B")
	if err != nil {
		t.Fatal(err)
	}
	o2 := newOwners(s2, 30*time.Second)
	if _, ok := o2.owner("M", now.Add(37*time.Second)); ok {
		t.Fatal("disk claim became live lease")
	}
	if err = configureOwners(o, 2, map[string][]string{}, relays); err != nil {
		t.Fatal(err)
	}
	if _, err = o.accept("B", sn(n), now.Add(38*time.Second)); !errors.Is(err, errUnauthorized) {
		t.Fatal("revoked notice accepted", err)
	}
	if err = configureOwners(o, 3, allowed, relays); err != nil {
		t.Fatal(err)
	}
	if _, err = o.accept("B", sn(n), now.Add(39*time.Second)); !errors.Is(err, errUnauthorized) {
		t.Fatal("old generation revived", err)
	}
	n.Generation = 3
	n.Seq++
	if _, err = o.accept("C", sn(n), now.Add(40*time.Second)); err != nil {
		t.Fatal(err)
	}
}

func TestSequencePersistsAndMissingStateFailsClosed(t *testing.T) {
	dir := t.TempDir()
	s, err := openStore(dir, "n", "M")
	if err != nil {
		t.Fatal(err)
	}
	a, err := s.nextSequence()
	if err != nil {
		t.Fatal(err)
	}
	if err = UpdateState(filepath.Join(dir, "state.json"), "endpoints", map[string]string{"A": "127.0.0.1:1"}); err != nil {
		t.Fatal(err)
	}
	s, err = openStore(dir, "n", "M")
	if err != nil {
		t.Fatal(err)
	}
	b, err := s.nextSequence()
	if err != nil || b <= a {
		t.Fatal(a, b, err)
	}
	if err = os.Remove(filepath.Join(dir, "state.json")); err != nil {
		t.Fatal(err)
	}
	if _, err = openStore(dir, "n", "M"); err == nil {
		t.Fatal("lost high water reset")
	}
}

func TestFailedPersistenceDoesNotCommit(t *testing.T) {
	s, _ := openStore("", "n", "M")
	s.save = func(persistent) error { return errors.New("disk full") }
	if _, err := s.nextSequence(); err == nil || s.state.Sequence != 0 {
		t.Fatal("committed unsaved sequence")
	}
}

func TestNoticeFromRelayAtAnotherRevision(t *testing.T) {
	s, err := openStore("", "n", "B")
	if err != nil {
		t.Fatal(err)
	}
	o := newOwners(s, 30*time.Second)
	allowed := map[string][]string{"M": {"A", "B"}}
	relays := map[string]bool{"A": true, "B": true}
	if err = configureOwners(o, 3, allowed, relays); err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	if _, err = o.accept("A", sn(Notice{Member: "M", Owner: "A", Seq: 1, Refresh: 1, Generation: 2, LeaseMs: 30000}), now); err != nil {
		t.Fatal("notice from a relay on an older revision", err)
	}
	if _, err = o.accept("A", sn(Notice{Member: "M", Owner: "A", Seq: 1, Refresh: 2, Generation: 4, LeaseMs: 30000}), now); err != nil {
		t.Fatal("notice from a relay on a newer revision", err)
	}
	if err = configureOwners(o, 5, map[string][]string{"M": {"B"}}, relays); err != nil {
		t.Fatal(err)
	}
	if _, err = o.accept("A", sn(Notice{Member: "M", Owner: "B", Seq: 2, Refresh: 1, Generation: 4, LeaseMs: 30000}), now); !errors.Is(err, errUnauthorized) {
		t.Fatal("notice from before the revocation accepted", err)
	}
	if _, err = o.accept("A", sn(Notice{Member: "M", Owner: "B", Seq: 2, Refresh: 1, Generation: 5, LeaseMs: 30000}), now); err != nil {
		t.Fatal(err)
	}
}
