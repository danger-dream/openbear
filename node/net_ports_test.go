//go:build linux || darwin || windows

package node

import (
	"bufio"
	"context"
	"encoding/json"
	"io"
	"log"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"
)

func callPorts(t *testing.T, a *Agent, params string) (map[string]map[string]portState, error) {
	t.Helper()
	r, _, err := a.dispatch(context.Background(), envelope{Method: "net.ports", Params: json.RawMessage(params)})
	if err != nil {
		return nil, err
	}
	data, _ := json.Marshal(r)
	var out map[string]map[string]portState
	if err := json.Unmarshal(data, &out); err != nil {
		t.Fatal(err)
	}
	return out, nil
}

func portsAgent(t *testing.T) *Agent {
	a := NewAgent(Config{}, log.New(io.Discard, "", 0))
	a.meshHost = newMeshHost(t.TempDir(), a.executable, a.build)
	return a
}

func TestNetPortsRejectsInvalidParams(t *testing.T) {
	a := portsAgent(t)
	many := strings.Repeat("1,", 32) + "2"
	for _, p := range []string{
		``, `null`, `[80]`, `{"tcp":80}`, `{"tcp":[0]}`, `{"udp":[65536]}`, `{"tcp":[-1]}`, `{"tcp":[1.5]}`,
		`{"tcp":["80"]}`, `{"tcp":[null]}`, `{"tcp":[` + many + `]}`, `{"tcp":[1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16],"udp":[1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17]}`,
	} {
		if _, err := callPorts(t, a, p); err == nil || err.Error() != "invalid_params" {
			t.Errorf("%s: want invalid_params, got %v", p, err)
		}
	}
	got, err := callPorts(t, a, `{}`)
	if err != nil || got["tcp"] == nil || got["udp"] == nil || len(got["tcp"])+len(got["udp"]) != 0 {
		t.Fatalf("no ports: %v %v", got, err)
	}
	if tcp, udp, err := parsePortParams(json.RawMessage(`{"tcp":[443,80,443,65535],"udp":[1],"other":true}`)); err != nil ||
		len(tcp) != 3 || tcp[0] != 80 || tcp[2] != 65535 || len(udp) != 1 {
		t.Fatalf("parse: %v %v %v", tcp, udp, err)
	}
}

// freePort finds a port nothing listens on for either network.
func freePort(t *testing.T) int {
	for range 20 {
		l, err := net.Listen("tcp", ":0")
		if err != nil {
			t.Fatal(err)
		}
		port := l.Addr().(*net.TCPAddr).Port
		l.Close()
		if c, err := net.ListenPacket("udp", ":"+strconv.Itoa(port)); err == nil {
			c.Close()
			return port
		}
	}
	t.Fatal("no free port")
	return 0
}

// holder runs exe as a process listening on networks; it returns the
// process and its ports.
func holder(t *testing.T, exe string, networks ...string) (*exec.Cmd, []string) {
	cmd := exec.Command(exe, append([]string{"hold-ports"}, networks...)...)
	stdin, _ := cmd.StdinPipe()
	stdout, _ := cmd.StdoutPipe()
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { stdin.Close(); cmd.Wait() })
	line, err := bufio.NewReader(stdout).ReadString('\n')
	if err != nil {
		t.Fatalf("holder: %v", err)
	}
	return cmd, strings.Fields(line)
}

func TestNetPortsFreeAndHeld(t *testing.T) {
	a := portsAgent(t)
	free := freePort(t)

	// Held here, both dual-stack and IPv4 alone.
	l, err := net.Listen("tcp", ":0")
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	l4, err := net.Listen("tcp4", "0.0.0.0:0")
	if err != nil {
		t.Fatal(err)
	}
	defer l4.Close()
	u, err := net.ListenPacket("udp", ":0")
	if err != nil {
		t.Fatal(err)
	}
	defer u.Close()
	own := []int{l.Addr().(*net.TCPAddr).Port, l4.Addr().(*net.TCPAddr).Port}
	ownUDP := u.LocalAddr().(*net.UDPAddr).Port

	// Held by another program, and by the mesh service's copy of the binary.
	self, _ := os.Executable()
	other, otherPorts := holder(t, self, "tcp", "udp4")
	copyName := "openbear-mesh"
	if runtime.GOOS == "windows" {
		copyName += ".exe"
	}
	copyPath := filepath.Join(a.meshNode().dir, "bin", copyName)
	os.MkdirAll(filepath.Dir(copyPath), 0o755)
	data, err := os.ReadFile(self)
	if err != nil || os.WriteFile(copyPath, data, 0o755) != nil {
		t.Fatal(err)
	}
	mesh, meshPorts := holder(t, copyPath, "tcp", "udp")

	n := func(s string) int { v, _ := strconv.Atoi(s); return v }
	params, _ := json.Marshal(map[string][]int{
		"tcp": {free, own[0], own[1], n(otherPorts[0]), n(meshPorts[0])},
		"udp": {free, ownUDP, n(otherPorts[1]), n(meshPorts[1])},
	})
	got, err := callPorts(t, a, string(params))
	if err != nil {
		t.Fatal(err)
	}
	p := strconv.Itoa
	ownName := filepath.Base(self)
	if runtime.GOOS == "linux" {
		ownName = ownName[:min(len(ownName), 15)] // the kernel's comm
	}
	for _, c := range []struct {
		network, port string
		want          portState
	}{
		{"tcp", p(free), portState{Free: true}},
		{"udp", p(free), portState{Free: true}},
		{"tcp", p(own[0]), portState{Ours: true, Process: ownName, Pid: os.Getpid()}},
		{"tcp", p(own[1]), portState{Ours: true, Process: ownName, Pid: os.Getpid()}},
		{"udp", p(ownUDP), portState{Ours: true, Process: ownName, Pid: os.Getpid()}},
		{"tcp", otherPorts[0], portState{Process: ownName, Pid: other.Process.Pid}},
		{"udp", otherPorts[1], portState{Process: ownName, Pid: other.Process.Pid}},
		{"tcp", meshPorts[0], portState{Ours: true, Process: copyName, Pid: mesh.Process.Pid}},
		{"udp", meshPorts[1], portState{Ours: true, Process: copyName, Pid: mesh.Process.Pid}},
	} {
		if g := got[c.network][c.port]; g != c.want {
			t.Errorf("%s %s: got %+v, want %+v", c.network, c.port, g, c.want)
		}
	}
	if len(got["tcp"]) != 5 || len(got["udp"]) != 4 {
		t.Errorf("unexpected entries: %v", got)
	}
}

func TestNetPortsOwnership(t *testing.T) {
	dir := t.TempDir()
	bin := filepath.Join(dir, "openbear-mesh")
	os.WriteFile(bin, nil, 0o755)
	for _, c := range []struct {
		o       portOwner
		meshPid int
		want    bool
	}{
		{portOwner{pid: os.Getpid()}, 0, true},
		{portOwner{pid: 99999, exe: bin}, 0, true},
		{portOwner{pid: 99999, exe: filepath.Join(dir, ".", "openbear-mesh")}, 0, true},
		{portOwner{pid: 99999, exe: "/usr/sbin/nginx"}, 99999, false}, // a reused pid
		{portOwner{pid: 99999}, 99999, true},                          // path unknown: status.json's pid
		{portOwner{pid: 99999}, 0, false},
		{portOwner{pid: 99998}, 99999, false},
	} {
		if got := ownedByUs(c.o, bin, c.meshPid); got != c.want {
			t.Errorf("%+v with mesh pid %d: got %v", c.o, c.meshPid, got)
		}
	}
}

// lsof -F pcPn of -iTCP:8080,443 -iUDP:8080,5353, with f lines as macOS
// prints them and without, as other builds do.
func TestParseLsof(t *testing.T) {
	out := "p310\ncnginx\nf6\nPTCP\nn*:8080\nf7\nPTCP\nn[::1]:8080\nf8\nPTCP\nn10.0.0.2:8080->10.0.0.9:51000\nf9\nPUDP\nn*:8080\n" +
		"p311\ncnginx\nPTCP\nn*:8080\nPTCP\nn10.0.0.2:51001->10.0.0.9:443\n" +
		"p90\ncmDNSResponder\nPUDP\nn127.0.0.1:5353->224.0.0.251:5353\nnbad\nPICMP\nn*:5353\n"
	got := parseLsof([]byte(out))
	tcp, udp := got["tcp"], got["udp"]
	if o := tcp[8080]; len(o) != 2 || o[0] != (portOwner{pid: 310, name: "nginx"}) || o[1].pid != 311 {
		t.Fatalf("tcp 8080: %+v", o)
	}
	if o := udp[8080]; len(o) != 1 || o[0].pid != 310 {
		t.Fatalf("udp 8080: %+v", o)
	}
	if o := udp[5353]; len(o) != 1 || o[0].name != "mDNSResponder" {
		t.Fatalf("udp 5353: %+v", o)
	}
	// TCP connections, an accepted one or one to a port asked for, are not
	// listeners.
	if len(tcp) != 1 || len(udp) != 2 || len(got) != 2 {
		t.Fatalf("%+v", got)
	}
}

func TestNetPortsNeedNoConsent(t *testing.T) {
	a := portsAgent(t)
	for range cap(a.busy) {
		a.busy <- struct{}{}
	}
	if _, err := callPorts(t, a, `{"tcp":[`+strconv.Itoa(freePort(t))+`]}`); err != nil {
		t.Fatal(err)
	}
}

// From the 10-30 check: on macOS and Windows a wildcard bind succeeds beside a listener on one
// address, so a port only 127.0.0.1 listened on was reported free.
func TestAPortListenedOnOneAddressIsNotFree(t *testing.T) {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	port := l.Addr().(*net.TCPAddr).Port
	got := checkPorts(context.Background(), map[string][]int{"tcp": {port}}, func(portOwner) bool { return false })
	if st := got["tcp"][strconv.Itoa(port)]; st.Free {
		t.Fatalf("port %d reported free: %+v", port, st)
	}
}
