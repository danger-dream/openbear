package mesh

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"testing"
	"time"

	"openbear/node/mesh/wire"
)

// TestCompatNetnsMixedReleases runs three relays with dynamic routing, A-C
// a slower link than A-B-C, A and C as a next release (mesh protocol P..P+1,
// config schemas S..S+1, reading this release's schema S config) and B as
// this one (P..P). Health checks, relaying through B and route choice work
// across the releases, each pair at the newest protocol both speak. Then B
// is upgraded, every pair goes to P+1; then C is rolled back, A and B go
// back to P with it, and the mesh keeps working throughout.
//
// This keeps the version mechanism working for the releases after this
// one. Releases before this one (protocol 1) do not speak to it at all: a
// mesh is upgraded to protocol 2 as a whole.
func TestCompatNetnsMixedReleases(t *testing.T) {
	rtSkip(t)
	const old = ""
	p, s := wire.Protocol, ConfigSchema
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	l := newRTLab(t, []string{"A", "B", "C"}, []string{"A-B:10", "B-C:10", "A-C:60"},
		map[string]string{"A>B": "B", "A>C": "B", "B>A": "A", "B>C": "C", "C>A": "B", "C>B": "B"})
	release := map[string]string{}
	launch := func(id, which string) {
		raw, _ := json.Marshal(l.config(id))
		if err := Validate(raw); err != nil {
			t.Fatalf("%s: %v", id, err)
		}
		os.WriteFile(filepath.Join(l.dirs[id], "config.json"), raw, 0o600)
		os.Remove(filepath.Join(l.dirs[id], "status.json"))
		log, _ := os.OpenFile(filepath.Join(l.dirs[id], "mesh.log"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
		cmd := exec.Command("ip", "netns", "exec", l.ns[id], self, "-test.run", "^TestNetnsNode$", "-test.v")
		cmd.Env = append(os.Environ(), "OBLAB_MESH_DIR="+l.dirs[id])
		if which == "next" {
			cmd.Env = append(cmd.Env, fmt.Sprintf("OBLAB_PROTOCOL=%d,%d", p, p+1), fmt.Sprintf("OBLAB_CONFIG_SCHEMAS=%d,%d", s, s+1))
		}
		cmd.Stdout, cmd.Stderr = log, log
		if err := cmd.Start(); err != nil {
			t.Fatal(err)
		}
		l.procs[id] = cmd
		release[id] = which
	}
	stopOne := func(id string) {
		cmd := l.procs[id]
		cmd.Process.Signal(os.Interrupt)
		done := make(chan error, 1)
		go func() { done <- cmd.Wait() }()
		select {
		case <-done:
		case <-time.After(10 * time.Second):
			cmd.Process.Kill()
			t.Errorf("%s did not stop on SIGINT", id)
		}
		delete(l.procs, id)
	}
	peerProtocol := func(id, peer string) int {
		for _, p := range l.status(id).Peers {
			if p.ID == peer {
				return p.Protocol
			}
		}
		return -1
	}
	t.Cleanup(func() {
		if t.Failed() {
			for _, id := range l.ids {
				s := l.status(id)
				raw, _ := json.Marshal(s.Peers)
				t.Logf("%s (%s) protocols %v schemas %v peers %s events %+v", id, release[id], s.Protocols, s.ConfigSchemas, raw, s.Events)
				log, _ := os.ReadFile(filepath.Join(l.dirs[id], "mesh.log"))
				t.Logf("%s log:\n%s", id, tail(log, 3000))
			}
		}
	})
	// expect waits until every pair speaks the protocol given, "A-B" both ways.
	expect := func(stage string, want map[string]int) {
		t.Helper()
		start := time.Now()
		l.waitFor(stage+": protocols", 45*time.Second, func() bool {
			for pair, v := range want {
				a, b := pair[:1], pair[2:]
				if peerProtocol(a, b) != v && (old == "" || release[a] != "this") {
					return false
				}
				if peerProtocol(b, a) != v && (old == "" || release[b] != "this") {
					return false
				}
			}
			return true
		})
		for _, id := range l.ids {
			st := l.status(id)
			protocols, schemas := [2]int{p, p}, [2]int{s, s}
			if release[id] == "next" {
				protocols, schemas = [2]int{p, p + 1}, [2]int{s, s + 1}
			}
			if st.Protocols != protocols || st.ConfigSchemas != schemas {
				t.Fatalf("%s: %s reports protocols %v, config schemas %v; want %v, %v", stage, id, st.Protocols, st.ConfigSchemas, protocols, schemas)
			}
		}
		t.Logf("%s: protocols A-B %d, B-C %d, A-C %d after %.1fs", stage, peerProtocol("A", "B"), peerProtocol("B", "C"), peerProtocol("A", "C"), time.Since(start).Seconds())
	}
	received := regexp.MustCompile(`(\d+) received`)
	// works checks the routes go through B and pings over the mesh, the
	// ones between A and C relayed by B, all come back.
	works := func(stage string) {
		t.Helper()
		l.paths(60*time.Second, "A>B", "B>A", "B>C", "C>B", "A>B>C", "C>B>A")
		for _, p := range [][2]string{{"A", "C"}, {"C", "A"}, {"A", "B"}, {"B", "C"}} {
			out, _ := execIn(l.ns[p[0]], "ping", "-n", "-q", "-c", "20", "-i", "0.1", "-W", "1", l.addr[p[1]].String())
			n := 0
			if m := received.FindStringSubmatch(out); m != nil {
				n, _ = strconv.Atoi(m[1])
			}
			if n < 20 {
				t.Fatalf("%s: %s>%s: %s", stage, p[0], p[1], out)
			}
		}
		for _, id := range l.ids {
			for _, e := range l.status(id).Events {
				if e.Kind == "routing.error" {
					t.Errorf("%s: %s (%s): routing error %s", stage, id, release[id], e.Detail)
				}
			}
		}
	}

	launch("A", "next")
	launch("B", "this")
	launch("C", "next")
	expect("mixed", map[string]int{"A-B": p, "B-C": p, "A-C": p + 1})
	works("mixed")

	stopOne("B")
	time.Sleep(2 * time.Second)
	launch("B", "next")
	expect("B upgraded", map[string]int{"A-B": p + 1, "B-C": p + 1, "A-C": p + 1})
	works("B upgraded")

	stopOne("C")
	time.Sleep(2 * time.Second)
	launch("C", "this")
	expect("C rolled back", map[string]int{"A-B": p + 1, "B-C": p, "A-C": p})
	works("C rolled back")
}
