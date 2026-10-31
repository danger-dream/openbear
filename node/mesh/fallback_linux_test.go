package mesh

import (
	"encoding/json"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"
)

// TestRestartWithUnappliedConfig needs root; OBLAB_FALLBACK=1 runs it. A
// relay R is given a revision it cannot apply, a TCP port another program
// holds: it keeps running the revision before and says why. It then
// restarts, as at a reboot, with that revision still in config.json: it
// comes back on the revision it last applied rather than staying down, and
// takes the new one when, the port free, the controller sends it again.
func TestRestartWithUnappliedConfig(t *testing.T) {
	if os.Getenv("OBLAB_FALLBACK") != "1" || os.Getuid() != 0 {
		t.Skip("set OBLAB_FALLBACK=1 and run as root")
	}
	l := newNSLab(t, "fb", "R", "M")
	l.veth("R", "r", "10.209.0.1/24", "M", "m", "10.209.0.2/24")
	l.node("R", roleRelay, 1)
	l.node("M", roleMember, 11, "R")
	r := l.config("R", Listen{UDP: 51820})
	r.Routing = &Routing{Mode: "static"}
	m := l.config("M", Listen{UDP: 51820})
	m.Entry = &Entry{Mode: "fixed", Candidates: []Candidate{{ID: "c1", Relay: "R", Endpoint: udpEndpoint("10.209.0.1", 51820)}}}
	l.run(map[string]*Config{"R": r, "M": m})
	reached := func() bool { _, rtt, _ := l.pings("M", "R", 1); return rtt > 0 }
	l.wait("M reaching R", 60*time.Second, reached)

	holder := exec.Command("ip", "netns", "exec", l.ns["R"], "socat", "TCP-LISTEN:52000,reuseaddr,fork", "SYSTEM:true")
	if err := holder.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { holder.Process.Kill(); holder.Wait() }()
	time.Sleep(300 * time.Millisecond)
	r.Revision = 2
	r.Self.Listen.TCP = 52000
	raw, err := json.Marshal(r)
	if err != nil {
		t.Fatal(err)
	}
	os.WriteFile(l.dirs["R"]+"/config.json.tmp", raw, 0o600)
	os.Rename(l.dirs["R"]+"/config.json.tmp", l.dirs["R"]+"/config.json")
	l.wait("R refusing revision 2", 30*time.Second, func() bool {
		s := l.status("R")
		return s.ConfigRevision == 2 && s.AppliedRevision == 1 && strings.Contains(s.Error, "revision 2")
	})
	if !reached() {
		t.Fatal("R stopped forwarding on a revision it could not apply")
	}

	self, _ := os.Executable()
	l.stopNode("R")
	os.Remove(l.dirs["R"] + "/status.json")
	l.startNode("R", self)
	l.wait("R back on revision 1 after a restart", 30*time.Second, func() bool {
		s := l.status("R")
		return s.AppliedRevision == 1 && s.ConfigRevision == 2 && strings.Contains(s.Error, "revision 2")
	})
	l.wait("M reaching R again", 15*time.Second, reached)

	// The port is freed and the controller retries: the same revision
	// written again is applied at once, not at the next retry 30 s later.
	holder.Process.Kill()
	holder.Wait()
	os.WriteFile(l.dirs["R"]+"/config.json.tmp", raw, 0o600)
	os.Rename(l.dirs["R"]+"/config.json.tmp", l.dirs["R"]+"/config.json")
	l.wait("R taking revision 2 when it is written again", 8*time.Second, func() bool {
		s := l.status("R")
		return s.AppliedRevision == 2 && s.Error == ""
	})
	l.wait("M reaching R on revision 2", 60*time.Second, reached)
}
