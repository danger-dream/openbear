package mesh

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"openbear/node/mesh/transport"
)

// TestContractKeyless runs TestContract's mesh, from configs the
// controller's own code builds, with m2's signing and end-to-end keys not
// known to the controller (an old data plane, one fallen back after a
// failed update, one not reported yet). Every other node must take its
// config and work as before, m1 and m3 end to end through both relays;
// m2, running anyway with keys of its own, reaches no one and no one
// takes it. Once the controller has its keys and issues the configs
// again, m2 is back. OBLAB_CONTRACT=1 enables it; it needs root and the
// repository's .venv.
func TestContractKeyless(t *testing.T) {
	if os.Getenv("OBLAB_CONTRACT") != "1" || os.Getuid() != 0 {
		t.Skip("set OBLAB_CONTRACT=1 and run as root")
	}
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	wd, _ := os.Getwd()
	repo := filepath.Clean(filepath.Join(wd, "..", ".."))
	ids := []string{"r1", "r2", "m1", "m2", "m3"}
	ns := map[string]string{"br": "oblab-kbr", "lan": "oblab-klan"}
	for _, id := range ids {
		ns[id] = "oblab-k" + id
	}
	sh := func(args ...string) {
		t.Helper()
		if out, err := exec.Command(args[0], args[1:]...).CombinedOutput(); err != nil {
			t.Fatalf("%s: %v: %s", strings.Join(args, " "), err, out)
		}
	}
	in := func(id string, args ...string) (string, error) {
		out, err := exec.Command("ip", append([]string{"netns", "exec", ns[id]}, args...)...).CombinedOutput()
		return string(out), err
	}
	cleanup := func() {
		for _, n := range ns {
			exec.Command("ip", "netns", "del", n).Run()
		}
	}
	cleanup()
	linksLeft := rootLinksLeft(t, "obl", "obmesh")
	t.Cleanup(func() {
		cleanup()
		if out, _ := exec.Command("ip", "netns", "list").Output(); bytes.Contains(out, []byte("oblab-k")) {
			t.Errorf("namespaces left behind: %s", out)
		}
		linksLeft()
	})
	for _, n := range ns {
		sh("ip", "netns", "add", n)
		sh("ip", "-n", n, "link", "set", "lo", "up")
	}
	sh("ip", "-n", ns["br"], "link", "add", "br0", "type", "bridge")
	sh("ip", "-n", ns["br"], "link", "set", "br0", "up")
	underlay := map[string]string{}
	for i, id := range ids {
		port := "oblk" + id
		underlay[id] = fmt.Sprintf("10.97.0.%d", i+1)
		sh("ip", "link", "add", port, "netns", ns["br"], "type", "veth", "peer", "name", "obleth", "netns", ns[id])
		sh("ip", "-n", ns["br"], "link", "set", port, "master", "br0", "up")
		sh("ip", "-n", ns[id], "addr", "add", underlay[id]+"/24", "dev", "obleth")
		sh("ip", "-n", ns[id], "link", "set", "obleth", "up")
	}
	sh("ip", "link", "add", "obllank", "netns", ns["m3"], "type", "veth", "peer", "name", "obleth", "netns", ns["lan"])
	sh("ip", "-n", ns["m3"], "addr", "add", "10.98.0.1/24", "dev", "obllank")
	sh("ip", "-n", ns["m3"], "link", "set", "obllank", "up")
	sh("ip", "-n", ns["lan"], "addr", "add", "10.98.0.2/24", "dev", "obleth")
	sh("ip", "-n", ns["lan"], "link", "set", "obleth", "up")

	dirs, keys := map[string]string{}, map[string][4]string{}
	for _, id := range ids {
		dirs[id] = t.TempDir()
		wgPriv, wgPub := newKey(t)
		ctlPriv, ctlPub := newKey(t)
		keys[id] = [4]string{wgPub, ctlPub, testSignPub(id), testE2EPub(id)}
		os.MkdirAll(filepath.Join(dirs[id], "keys"), 0o700)
		os.WriteFile(filepath.Join(dirs[id], "keys", "wg.key"), []byte(wgPriv), 0o600)
		os.WriteFile(filepath.Join(dirs[id], "keys", "control.key"), []byte(ctlPriv), 0o600)
		// m2 has keys of its own, as a current data plane does: only the
		// controller does not know them yet.
		writeTestKeys(t, dirs[id], id)
	}
	certs, err := transport.EnsureCertificate(filepath.Join(dirs["r2"], "tls"))
	if err != nil {
		t.Fatal(err)
	}

	addr := map[string]string{}
	configs := map[string]*Config{}
	generate := func(revision int, keyless ...string) {
		t.Helper()
		work := t.TempDir()
		input, _ := json.Marshal(map[string]any{"keys": keys, "ipv4": underlay, "relayTransport": "udp", "revision": revision, "configSchemas": ConfigSchemas(),
			"tlsFingerprint": map[string]string{"r2": certs.Active.FingerprintSHA256}, "keyless": keyless})
		os.WriteFile(filepath.Join(work, "in.json"), input, 0o600)
		cmd := exec.Command(filepath.Join(repo, ".venv", "bin", "python"), "node/mesh/testdata/contract_configs.py",
			filepath.Join(work, "in.json"), filepath.Join(work, "out.json"))
		cmd.Dir = repo
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("controller config build: %v: %s", err, out)
		}
		var got struct {
			Configs map[string]json.RawMessage `json:"configs"`
			Errors  []map[string]any           `json:"errors"`
		}
		raw, _ := os.ReadFile(filepath.Join(work, "out.json"))
		if err := json.Unmarshal(raw, &got); err != nil {
			t.Fatal(err)
		}
		// The draft check names a member without keys (apply waits for it);
		// the configs the controller issues meanwhile must still be taken.
		for _, e := range got.Errors {
			if e["error"] != "mesh_keys_missing" {
				t.Fatalf("the controller refuses its own draft: %v", got.Errors)
			}
		}
		if len(got.Errors) != len(keyless) {
			t.Fatalf("draft check %v, want mesh_keys_missing for %v", got.Errors, keyless)
		}
		for _, id := range ids {
			c, err := ParseConfig(got.Configs[id])
			if err != nil {
				t.Fatalf("the data plane refuses the controller's config for %s: %v\n%s", id, err, got.Configs[id])
			}
			configs[id] = c
			addr[id] = c.Self.Address.String()
			path := filepath.Join(dirs[id], "config.json")
			os.WriteFile(path+".tmp", got.Configs[id], 0o600)
			if err := os.Rename(path+".tmp", path); err != nil {
				t.Fatal(err)
			}
		}
	}
	status := func(id string) Status {
		var s Status
		raw, _ := os.ReadFile(filepath.Join(dirs[id], "status.json"))
		json.Unmarshal(raw, &s)
		return s
	}
	waitFor := func(what string, within time.Duration, ok func() bool) {
		t.Helper()
		for deadline := time.Now().Add(within); !ok(); time.Sleep(200 * time.Millisecond) {
			if time.Now().After(deadline) {
				t.Fatalf("timed out waiting for %s", what)
			}
		}
	}
	reaches := func(from, to string) bool {
		for try := 0; try < 3; try++ { // the first packet may wait for handshakes
			if _, err := in(from, "ping", "-c", "1", "-W", "2", addr[to]); err == nil {
				return true
			}
		}
		return false
	}
	expect := func(from, to string) {
		t.Helper()
		if !reaches(from, to) {
			t.Fatalf("%s does not reach %s", from, to)
		}
	}
	refused := func(from, to string) {
		t.Helper()
		if _, err := in(from, "ping", "-c", "2", "-W", "1", addr[to]); err == nil {
			t.Fatalf("%s reached %s", from, to)
		}
	}
	peerOf := func(id, peer string) *PeerStatus {
		for _, p := range status(id).Peers {
			if p.ID == peer {
				return &p
			}
		}
		return nil
	}

	generate(1, "m2")
	for _, id := range []string{"r1", "r2", "m1", "m3"} {
		for _, n := range configs[id].Nodes {
			if n.ID == "m2" {
				t.Fatalf("%s's config names m2, whose keys the controller does not have", id)
			}
		}
	}
	procs := map[string]*exec.Cmd{}
	for _, id := range ids {
		log, _ := os.Create(filepath.Join(dirs[id], "mesh.log"))
		cmd := exec.Command("ip", "netns", "exec", ns[id], self, "-test.run", "^TestNetnsNode$", "-test.v")
		cmd.Env = append(os.Environ(), "OBLAB_MESH_DIR="+dirs[id])
		cmd.Stdout, cmd.Stderr = log, log
		if err := cmd.Start(); err != nil {
			t.Fatal(err)
		}
		procs[id] = cmd
	}
	t.Cleanup(func() {
		for id, cmd := range procs {
			cmd.Process.Signal(os.Interrupt)
			done := make(chan error, 1)
			go func() { done <- cmd.Wait() }()
			select {
			case <-done:
			case <-time.After(10 * time.Second):
				cmd.Process.Kill()
				t.Errorf("%s did not stop on SIGINT", id)
			}
		}
		if t.Failed() {
			for _, id := range ids {
				raw, _ := os.ReadFile(filepath.Join(dirs[id], "mesh.log"))
				st, _ := os.ReadFile(filepath.Join(dirs[id], "status.json"))
				t.Logf("%s log:\n%s\n%s status:\n%s", id, tail(raw, 6000), id, st)
			}
		}
	})
	for _, id := range []string{"r1", "r2", "m1", "m3"} {
		waitFor(id+" ready", 30*time.Second, func() bool { s := status(id); return s.Ready && s.AppliedRevision == 1 && s.Error == "" })
	}
	waitFor("m2 running revision 1", 30*time.Second, func() bool { s := status("m2"); return s.AppliedRevision == 1 && s.Error == "" })

	// Everyone else as before: m1 and m3 through r1 and r2, end to end.
	sent, forwarded := status("m1").E2E.Sent, status("r1").Counters.Forwarded+status("r2").Counters.Forwarded
	expect("m1", "m3")
	expect("m3", "m1")
	waitFor("the pings to go end to end through the relays", 10*time.Second, func() bool {
		return status("m1").E2E.Sent > sent && status("r1").Counters.Forwarded+status("r2").Counters.Forwarded > forwarded
	})
	// m2 tries its entry r1 and its direct link to m1; no one has it, so no
	// handshake completes and nothing from it is taken.
	time.Sleep(5 * time.Second)
	refused("m1", "m2")
	refused("m2", "m1")
	refused("m2", "m3")
	for _, id := range []string{"r1", "m1"} {
		if p := peerOf(id, "m2"); p != nil {
			t.Fatalf("%s has m2 as a peer: %+v", id, *p)
		}
	}
	if s := status("m2"); s.Ready {
		t.Fatalf("m2 is ready though no one has it: %+v", s.Entry)
	}
	if p := peerOf("m2", "r1"); p == nil || p.Handshakes != 0 {
		t.Fatalf("m2's entry r1 answered: %+v", p)
	}
	if s := status("m1"); s.Security == nil || s.Security.Signing.Rejected != 0 {
		t.Fatalf("m1's signature checks: %+v", s.Security)
	}
	expect("m1", "m3") // still

	// The controller learns m2's keys and issues the configs again: m2 is back.
	generate(2)
	for _, id := range ids {
		waitFor(id+" ready on revision 2", 60*time.Second, func() bool { s := status(id); return s.Ready && s.AppliedRevision == 2 && s.Error == "" })
	}
	expect("m1", "m2")
	expect("m2", "m1")
	sent = status("m2").E2E.Sent
	expect("m2", "m3")
	expect("m3", "m2")
	waitFor("m2 to reach m3 end to end", 10*time.Second, func() bool { return status("m2").E2E.Sent > sent })
	expect("m1", "m3")
}
