package node

import (
	"bufio"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"net/http"
	"net/netip"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"openbear/node/mesh"
)

// The control connection over the mesh, in two oblab- namespaces: K, the
// controller's machine, a mesh relay at 100.96.0.1 running a stand-in
// controller on :18961; N, a member at 100.96.0.2 whose node agent has the
// controller's mesh address and its direct one, 10.88.0.1. Each process is
// this test binary: the data planes are mesh.RunService, N's run by the
// agent through procService, a stand-in for systemd that like the real unit
// restarts the data plane 5 seconds after it exits.

// TestNetnsMeshDataPlane is a data plane process.
func TestNetnsMeshDataPlane(t *testing.T) {
	dir := os.Getenv("OBLAB_MESH_DIR")
	if dir == "" {
		t.Skip("run by TestNetnsControlOverMesh")
	}
	if err := mesh.RunService(dir); err != nil {
		t.Fatal(err)
	}
}

func fakeFromSeeds(s string) *fakeController {
	parts := strings.Split(s, ",")
	a, _ := b64.DecodeString(parts[0])
	b, _ := b64.DecodeString(parts[1])
	key, nodeK := ed25519.NewKeyFromSeed(a), ed25519.NewKeyFromSeed(b)
	return &fakeController{key: key, node: nodeK.Public().(ed25519.PublicKey), nodeK: nodeK}
}

type cmEvent struct {
	At   int64  `json:"at"` // Unix ms
	Via  string `json:"via"`
	Kind string `json:"kind"`
	Data any    `json:"data,omitempty"`
}

// TestNetnsController is the stand-in controller: it logs every connection
// to events.jsonl, "mesh" or "direct" by the address it came to, and sends
// the calls the test drops into its directory to the session up now.
func TestNetnsController(t *testing.T) {
	dir := os.Getenv("OBLAB_CM_CONTROLLER")
	if dir == "" {
		t.Skip("run by TestNetnsControlOverMesh")
	}
	f := fakeFromSeeds(os.Getenv("OBLAB_CM_KEYS"))
	out, err := os.OpenFile(filepath.Join(dir, "events.jsonl"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	var mu sync.Mutex
	var current *channel
	emit := func(via, kind string, data any) {
		line, _ := json.Marshal(cmEvent{At: time.Now().UnixMilli(), Via: via, Kind: kind, Data: data})
		mu.Lock()
		out.Write(append(line, '\n'))
		mu.Unlock()
	}
	handler := func(w http.ResponseWriter, r *http.Request) {
		via := "direct"
		if local, _ := r.Context().Value(http.LocalAddrContextKey).(net.Addr); local != nil && strings.HasPrefix(local.String(), "100.96.") {
			via = "mesh"
		}
		ch, auth := f.handshake(t, w, r, nil)
		if ch == nil {
			return
		}
		if auth["probe"] == true {
			emit(via, "probe", nil)
			return
		}
		ch.conn.SetIdleTimeout(2 * time.Minute)
		for {
			k, data, err := ch.read()
			if err != nil {
				emit(via, "closed", nil)
				return
			}
			var m map[string]any
			if k != kindJSON || json.Unmarshal(data, &m) != nil {
				continue
			}
			switch m["t"] {
			case "hello":
				mu.Lock()
				current = ch
				mu.Unlock()
				emit(via, "hello", map[string]any{"backup": m["backup"], "endpoint": m["endpoint"]})
			case "reply":
				emit(via, "reply", m)
			}
		}
	}
	go (&http.Server{Addr: "0.0.0.0:18961", Handler: http.HandlerFunc(handler)}).ListenAndServe()
	for {
		matches, _ := filepath.Glob(filepath.Join(dir, "call-*.json"))
		for _, path := range matches {
			data, _ := os.ReadFile(path)
			os.Remove(path)
			var c map[string]any
			json.Unmarshal(data, &c)
			mu.Lock()
			ch := current
			mu.Unlock()
			if ch != nil {
				ch.send(c)
				emit("", "called", c["id"])
			}
		}
		time.Sleep(100 * time.Millisecond)
	}
}

// procService runs the data plane as a child process, restarting it 5
// seconds after an exit it did not ask for, as Restart=always does.
type procService struct {
	mu       sync.Mutex
	bin, dir string
	cmd      *exec.Cmd
	exited   chan struct{}
	wanted   bool
}

func (s *procService) install(bin, dir string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.bin, s.dir = bin, dir
	return nil
}

func (s *procService) state() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	switch {
	case s.bin == "":
		return "absent"
	case s.cmd != nil:
		return "running"
	}
	return "stopped"
}

func (s *procService) start() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.wanted = true
	if s.cmd != nil {
		return nil
	}
	return s.spawnLocked()
}

func (s *procService) spawnLocked() error {
	logFile, _ := os.OpenFile(filepath.Join(s.dir, "mesh.log"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	cmd := exec.Command(s.bin, "-test.run", "^TestNetnsMeshDataPlane$", "-test.v")
	cmd.Env = append(os.Environ(), "OBLAB_MESH_DIR="+s.dir)
	cmd.Stdout, cmd.Stderr = logFile, logFile
	if err := cmd.Start(); err != nil {
		return err
	}
	done := make(chan struct{})
	s.cmd, s.exited = cmd, done
	go func() {
		cmd.Wait()
		logFile.Close()
		close(done)
		s.mu.Lock()
		defer s.mu.Unlock()
		if s.cmd != cmd {
			return
		}
		s.cmd = nil
		if s.wanted {
			time.AfterFunc(5*time.Second, func() {
				s.mu.Lock()
				defer s.mu.Unlock()
				if s.wanted && s.cmd == nil {
					s.spawnLocked()
				}
			})
		}
	}()
	return nil
}

func (s *procService) stop() error {
	s.mu.Lock()
	s.wanted = false
	cmd, done := s.cmd, s.exited
	s.mu.Unlock()
	if cmd == nil {
		return nil
	}
	cmd.Process.Signal(os.Interrupt)
	select {
	case <-done:
	case <-time.After(20 * time.Second):
		cmd.Process.Kill()
		<-done
	}
	return nil
}

func (s *procService) remove() error {
	s.stop()
	s.mu.Lock()
	defer s.mu.Unlock()
	s.bin = ""
	return nil
}

// TestNetnsAgent is N's node agent.
func TestNetnsAgent(t *testing.T) {
	root := os.Getenv("OBLAB_CM_AGENT")
	if root == "" {
		t.Skip("run by TestNetnsControlOverMesh")
	}
	f := fakeFromSeeds(os.Getenv("OBLAB_CM_KEYS"))
	a := NewAgent(f.config(os.Getenv("OBLAB_CM_PRIMARY"), Capabilities{Exec: true}), log.New(os.Stderr, "agent: ", log.Lmicroseconds))
	a.Config.Backup = os.Getenv("OBLAB_CM_BACKUP")
	exe := os.Getenv("OBLAB_CM_EXE")
	h := newMeshHost(filepath.Join(root, "mesh"), func() (string, error) { return exe, nil }, func() string {
		sum, _ := fileHash(exe)
		return sum
	})
	svc := &procService{}
	h.service = svc
	a.meshHost = h
	if _, err := h.install(); err != nil {
		t.Fatal(err)
	}
	delay, _ := time.ParseDuration(os.Getenv("OBLAB_CM_MESH_DELAY"))
	time.AfterFunc(delay, func() { svc.start() })
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	a.Run(ctx)
	svc.stop()
}

type cmLab struct {
	t        *testing.T
	self     string
	root     string
	ctlDir   string
	keys     string
	agent    *exec.Cmd
	agentLog string
	cursor   int
}

const (
	cmK, cmN     = "oblab-cmk", "oblab-cmn"
	cmMeshURL    = "http://100.96.0.1:18961"
	cmDirectURL  = "http://10.88.0.1:18961"
	cmStopWithin = 3 * time.Second
)

func (l *cmLab) sh(args ...string) string {
	l.t.Helper()
	out, err := exec.Command(args[0], args[1:]...).CombinedOutput()
	if err != nil {
		l.t.Fatalf("%s: %v: %s", strings.Join(args, " "), err, out)
	}
	return string(out)
}

func (l *cmLab) spawn(ns string, logPath string, env []string, run string) *exec.Cmd {
	l.t.Helper()
	logFile, err := os.OpenFile(logPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		l.t.Fatal(err)
	}
	cmd := exec.Command("ip", "netns", "exec", ns, l.self, "-test.run", run, "-test.v")
	cmd.Env = append(os.Environ(), env...)
	cmd.Stdout, cmd.Stderr = logFile, logFile
	if err := cmd.Start(); err != nil {
		l.t.Fatal(err)
	}
	go func() { cmd.Wait(); logFile.Close() }()
	return cmd
}

func (l *cmLab) startAgent(primary, backup string, meshDelay time.Duration) {
	l.agent = l.spawn(cmN, l.agentLog, []string{"OBLAB_CM_AGENT=" + l.root, "OBLAB_CM_KEYS=" + l.keys, "OBLAB_CM_PRIMARY=" + primary,
		"OBLAB_CM_BACKUP=" + backup, "OBLAB_CM_EXE=" + filepath.Join(l.root, "openbear-node-next"), "OBLAB_CM_MESH_DELAY=" + meshDelay.String()},
		"^TestNetnsAgent$")
}

func (l *cmLab) stopAgent() {
	if l.agent == nil {
		return
	}
	l.agent.Process.Signal(os.Interrupt)
	for deadline := time.Now().Add(15 * time.Second); l.agent.ProcessState == nil && time.Now().Before(deadline); time.Sleep(100 * time.Millisecond) {
	}
	l.agent = nil
}

func (l *cmLab) events() []cmEvent {
	f, err := os.Open(filepath.Join(l.ctlDir, "events.jsonl"))
	if err != nil {
		return nil
	}
	defer f.Close()
	var out []cmEvent
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	for sc.Scan() {
		var e cmEvent
		if json.Unmarshal(sc.Bytes(), &e) == nil {
			out = append(out, e)
		}
	}
	return out
}

// await returns the first event after the cursor that is via/kind, moving
// the cursor past it.
func (l *cmLab) await(via, kind string, within time.Duration) cmEvent {
	l.t.Helper()
	deadline := time.Now().Add(within)
	for {
		all := l.events()
		for i := l.cursor; i < len(all); i++ {
			if all[i].Via == via && all[i].Kind == kind {
				l.cursor = i + 1
				return all[i]
			}
		}
		if time.Now().After(deadline) {
			l.t.Fatalf("no %s %s within %s; events since: %s", via, kind, within, l.describe(all[min(l.cursor, len(all)):]))
		}
		time.Sleep(50 * time.Millisecond)
	}
}

func (l *cmLab) describe(events []cmEvent) string {
	var parts []string
	for _, e := range events {
		parts = append(parts, fmt.Sprintf("%s %s %s", time.UnixMilli(e.At).Format("15:04:05.000"), e.Via, e.Kind))
	}
	return strings.Join(parts, ", ")
}

func (l *cmLab) status() meshStatus {
	var s meshStatus
	data, _ := os.ReadFile(filepath.Join(l.root, "mesh", "status.json"))
	json.Unmarshal(data, &s)
	return s
}

// ready waits for N's data plane to be ready, a process other than not.
func (l *cmLab) ready(not int, within time.Duration) (meshStatus, time.Time) {
	l.t.Helper()
	deadline := time.Now().Add(within)
	for {
		if s := l.status(); s.Ready && s.Pid > 0 && s.Pid != not {
			return s, time.Now()
		}
		if time.Now().After(deadline) {
			l.t.Fatalf("N's data plane not ready within %s: %+v", within, l.status())
		}
		time.Sleep(100 * time.Millisecond)
	}
}

func at(e cmEvent) time.Time { return time.UnixMilli(e.At) }

// TestNetnsControlOverMesh, with OBLAB_NETNS=1 as root: the agent falls to
// the direct address at once while the mesh is not up and returns when it
// is; moves to it before a mesh upgrade and back after; leaves at once when
// the data plane dies or its interface goes; and with the direct address as
// primary ignores the mesh going down.
func TestNetnsControlOverMesh(t *testing.T) {
	if os.Getenv("OBLAB_NETNS") != "1" || os.Getuid() != 0 {
		t.Skip("set OBLAB_NETNS=1 and run as root")
	}
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	l := &cmLab{t: t, self: self, root: t.TempDir(), ctlDir: t.TempDir()}
	l.agentLog = filepath.Join(l.root, "agent.log")
	cleanup := func() {
		for _, ns := range []string{cmK, cmN} {
			if out, err := exec.Command("ip", "netns", "pids", ns).Output(); err == nil {
				for _, pid := range strings.Fields(string(out)) {
					exec.Command("kill", "-9", pid).Run()
				}
			}
			exec.Command("ip", "netns", "del", ns).Run()
		}
	}
	cleanup()
	t.Cleanup(func() {
		l.stopAgent()
		if t.Failed() {
			for _, path := range []string{l.agentLog, filepath.Join(l.root, "mesh", "mesh.log")} {
				data, _ := os.ReadFile(path)
				t.Logf("%s:\n%s", path, tail(data, 6000))
			}
		}
		cleanup()
	})
	for _, ns := range []string{cmK, cmN} {
		l.sh("ip", "netns", "add", ns)
		l.sh("ip", "-n", ns, "link", "set", "lo", "up")
	}
	l.sh("ip", "link", "add", "oblcmk", "netns", cmK, "type", "veth", "peer", "name", "oblcmn", "netns", cmN)
	l.sh("ip", "-n", cmK, "addr", "add", "10.88.0.1/24", "dev", "oblcmk")
	l.sh("ip", "-n", cmN, "addr", "add", "10.88.0.2/24", "dev", "oblcmn")
	l.sh("ip", "-n", cmK, "link", "set", "oblcmk", "up")
	l.sh("ip", "-n", cmN, "link", "set", "oblcmn", "up")

	// Keys, configs, and N's data plane copy as the release before; the
	// node's own binary is the next build.
	kDir, nDir := t.TempDir(), filepath.Join(l.root, "mesh")
	pubs, controlPubs := map[string]string{}, map[string]string{}
	for id, dir := range map[string]string{"K": kDir, "N": nDir} {
		os.MkdirAll(filepath.Join(dir, "keys"), 0o700)
		if pubs[id], err = ensureMeshKey(filepath.Join(dir, "keys", "wg.key")); err != nil {
			t.Fatal(err)
		}
		if controlPubs[id], err = ensureMeshKey(filepath.Join(dir, "keys", "control.key")); err != nil {
			t.Fatal(err)
		}
	}
	addr := map[string]netip.Addr{"K": netip.MustParseAddr("100.96.0.1"), "N": netip.MustParseAddr("100.96.0.2")}
	ctl := map[string]netip.Addr{"K": netip.MustParseAddr("100.96.255.1"), "N": netip.MustParseAddr("100.96.255.2")}
	role := map[string]string{"K": "relay", "N": "member"}
	config := func(id string) []byte {
		other := map[string]string{"K": "N", "N": "K"}[id]
		peer := mesh.Node{ID: other, Name: other, Role: role[other], Address: addr[other], ControlAddress: ctl[other], PublicKey: pubs[other], ControlKey: controlPubs[other]}
		if other == "N" {
			peer.Relays = []string{"K"}
		}
		c := mesh.Config{Schema: 1, Revision: 1,
			Network: mesh.Network{ID: "oblab-cm", Prefix: netip.MustParsePrefix("100.96.0.0/16"), ControlPrefix: netip.MustParsePrefix("100.96.255.0/24"), MTU: 1400},
			Self:    mesh.Self{ID: id, Name: id, Role: role[id], Address: addr[id], ControlAddress: ctl[id], Listen: mesh.Listen{UDP: 51820}},
			Nodes:   []mesh.Node{peer}, Rules: []mesh.Rule{{Proto: "any"}}, Links: []mesh.Link{}}
		if id == "K" {
			c.NextHops = map[string]string{}
		} else {
			c.Entry = &mesh.Entry{Mode: "fixed", Candidates: []mesh.Candidate{{ID: "c1", Relay: "K", Endpoint: "udp://10.88.0.1:51820"}}}
		}
		raw, err := json.Marshal(c)
		if err != nil {
			t.Fatal(err)
		}
		if err := mesh.Validate(raw); err != nil {
			t.Fatalf("%s: %v", id, err)
		}
		return raw
	}
	os.WriteFile(filepath.Join(kDir, "config.json"), config("K"), 0o600)
	// N applied this config before: the agent knows the mesh's prefix from
	// the start, as after a reboot.
	os.WriteFile(filepath.Join(nDir, "config.json"), config("N"), 0o600)
	os.WriteFile(filepath.Join(nDir, "applied.json"), config("N"), 0o600)
	os.MkdirAll(filepath.Join(nDir, "bin"), 0o700)
	if err := copyExecutable(self, filepath.Join(nDir, "bin", "openbear-mesh")); err != nil {
		t.Fatal(err)
	}
	next := filepath.Join(l.root, "openbear-node-next")
	if err := copyExecutable(self, next); err != nil {
		t.Fatal(err)
	}
	if f, err := os.OpenFile(next, os.O_APPEND|os.O_WRONLY, 0); err == nil {
		f.Write([]byte("\nnext build\n"))
		f.Close()
	}
	ctlSeed, nodeSeed := make([]byte, 32), make([]byte, 32)
	rand.Read(ctlSeed)
	rand.Read(nodeSeed)
	l.keys = b64.EncodeToString(ctlSeed) + "," + b64.EncodeToString(nodeSeed)
	l.spawn(cmK, filepath.Join(kDir, "mesh.log"), []string{"OBLAB_MESH_DIR=" + kDir}, "^TestNetnsMeshDataPlane$")
	l.spawn(cmK, filepath.Join(l.ctlDir, "controller.log"), []string{"OBLAB_CM_CONTROLLER=" + l.ctlDir, "OBLAB_CM_KEYS=" + l.keys}, "^TestNetnsController$")
	time.Sleep(time.Second)

	// 1. Start with the mesh down: direct at once, the mesh address once
	// the data plane is ready.
	started := time.Now()
	l.startAgent(cmMeshURL, cmDirectURL, 4*time.Second)
	first := l.await("direct", "hello", 10*time.Second)
	t.Logf("start: on the direct address %s after the agent started", at(first).Sub(started).Round(time.Millisecond))
	if d := at(first).Sub(started); d > 3*time.Second {
		t.Errorf("start: %s to reach the direct address with the mesh down", d)
	}
	for _, e := range l.events()[:l.cursor] {
		if e.Via == "mesh" {
			t.Errorf("start: a connection over the mesh before it was up: %+v", e)
		}
	}
	s, readyAt := l.ready(0, 30*time.Second)
	home := l.await("mesh", "hello", 30*time.Second)
	t.Logf("start: back on the mesh address %s after the data plane was ready", at(home).Sub(readyAt).Round(time.Millisecond))
	if d := at(home).Sub(readyAt); d > 30*time.Second {
		t.Errorf("start: %s to return", d)
	}

	// 2. mesh.upgrade over the mesh session: direct first, then the upgrade,
	// the reply on the direct session, back once the new copy is ready.
	os.WriteFile(filepath.Join(l.ctlDir, "call-1.json"), []byte(`{"t":"call","id":"up-1","method":"mesh.upgrade","params":{}}`), 0o644)
	called := l.await("", "called", 5*time.Second)
	probe := l.await("direct", "probe", 10*time.Second)
	closed := l.await("mesh", "closed", 10*time.Second)
	moved := l.await("direct", "hello", 10*time.Second)
	reply := l.await("direct", "reply", 40*time.Second)
	if r, _ := reply.Data.(map[string]any); r["id"] != "up-1" || r["ok"] != true {
		t.Fatalf("upgrade reply: %v", reply.Data)
	}
	_, readyAt = l.ready(s.Pid, 5*time.Second)
	if copied, _ := fileHash(filepath.Join(nDir, "bin", "openbear-mesh")); copied != mustHash(t, next) {
		t.Fatal("the data plane copy was not replaced")
	}
	home = l.await("mesh", "hello", 35*time.Second)
	t.Logf("upgrade: call %s, probe +%s, mesh session closed +%s, direct hello +%s, reply +%s; back on the mesh %s after the new copy was ready",
		time.UnixMilli(called.At).Format("15:04:05.000"), at(probe).Sub(at(called)), at(closed).Sub(at(called)), at(moved).Sub(at(called)),
		at(reply).Sub(at(called)), at(home).Sub(readyAt).Round(time.Millisecond))
	if !at(moved).After(at(closed)) && at(moved) != at(closed) {
		t.Error("upgrade: direct hello before the mesh session closed")
	}
	if logged := string(mustRead(t, l.agentLog)); !strings.Contains(logged, "the mesh upgrade takes the mesh down; switching to the backup address") {
		t.Error("upgrade: the agent did not say it moved first")
	}

	// 3. The data plane dies: direct at once; back after the restart.
	s = l.status()
	killed := time.Now()
	syscall.Kill(s.Pid, syscall.SIGKILL)
	moved = l.await("direct", "hello", 10*time.Second)
	t.Logf("crash: on the direct address %s after the data plane was killed", at(moved).Sub(killed).Round(time.Millisecond))
	if d := at(moved).Sub(killed); d > cmStopWithin {
		t.Errorf("crash: %s to leave the mesh address", d)
	}
	s, readyAt = l.ready(s.Pid, 30*time.Second)
	home = l.await("mesh", "hello", 35*time.Second)
	t.Logf("crash: back on the mesh %s after the restarted data plane was ready", at(home).Sub(readyAt).Round(time.Millisecond))

	// 4. The interface goes while the process stays.
	gone := time.Now()
	l.sh("ip", "-n", cmN, "link", "del", "obmesh0")
	moved = l.await("direct", "hello", 10*time.Second)
	t.Logf("interface: on the direct address %s after obmesh0 was deleted", at(moved).Sub(gone).Round(time.Millisecond))
	if d := at(moved).Sub(gone); d > cmStopWithin {
		t.Errorf("interface: %s to leave the mesh address", d)
	}
	alive := false
	if _, ok := processStart(s.Pid); ok {
		alive = true
		syscall.Kill(s.Pid, syscall.SIGKILL) // nothing brings the interface back but a restart
	}
	t.Logf("interface: data plane still running after losing its interface: %v", alive)
	_, readyAt = l.ready(s.Pid, 30*time.Second)
	home = l.await("mesh", "hello", 35*time.Second)
	t.Logf("interface: back on the mesh %s after the restarted data plane was ready", at(home).Sub(readyAt).Round(time.Millisecond))

	// 5. Direct address as primary, the mesh address as backup: the mesh
	// going down does not touch the session.
	l.stopAgent()
	l.cursor = len(l.events())
	l.startAgent(cmDirectURL, cmMeshURL, 0)
	l.await("direct", "hello", 10*time.Second)
	s, _ = l.ready(0, 30*time.Second)
	mark := len(l.events())
	syscall.Kill(s.Pid, syscall.SIGKILL)
	time.Sleep(8 * time.Second)
	if later := l.events()[mark:]; len(later) > 0 {
		t.Errorf("non-mesh primary: the session moved when the mesh went down: %s", l.describe(later))
	}
	if strings.Contains(string(mustRead(t, l.agentLog)[agentLogSize(t, l):]), "mesh here is down") {
		t.Error("non-mesh primary: the agent reacted to the mesh")
	}
}

// agentLogSize is where the log of the agent started last begins.
func agentLogSize(t *testing.T, l *cmLab) int {
	data := mustRead(t, l.agentLog)
	if i := strings.LastIndex(string(data), "=== RUN   TestNetnsAgent"); i >= 0 {
		return i
	}
	return 0
}

func mustRead(t *testing.T, path string) []byte {
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func mustHash(t *testing.T, path string) string {
	sum, err := fileHash(path)
	if err != nil {
		t.Fatal(err)
	}
	return sum
}

func tail(data []byte, n int) string {
	if len(data) > n {
		data = data[len(data)-n:]
	}
	return string(data)
}
