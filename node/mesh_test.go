//go:build linux

package node

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net"
	"net/netip"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"openbear/node/mesh"
	"openbear/node/mesh/sign"
)

// fakeSystemd stands in for systemctl, so no test touches the machine's units.
type fakeSystemd struct {
	mu      sync.Mutex
	calls   []string
	active  bool
	onStart func()
}

func (f *fakeSystemd) run(args ...string) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, strings.Join(args, " "))
	switch args[0] {
	case "is-active":
		if f.active {
			return "active", nil
		}
		return "inactive", errors.New("exit status 3")
	case "start":
		f.active = true
		if f.onStart != nil {
			go f.onStart()
		}
	case "stop":
		f.active = false
	}
	return "", nil
}

func (f *fakeSystemd) called(prefix string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	n := 0
	for _, c := range f.calls {
		if strings.HasPrefix(c, prefix) {
			n++
		}
	}
	return n
}

func testMesh(t *testing.T) (*meshHost, *fakeSystemd, string) {
	t.Helper()
	root := t.TempDir()
	exe := filepath.Join(root, "openbear-node")
	if err := os.WriteFile(exe, []byte("build 1"), 0o755); err != nil {
		t.Fatal(err)
	}
	f := &fakeSystemd{}
	h := newMeshHost(filepath.Join(root, "mesh"), func() (string, error) { return exe, nil }, func() string {
		sum, _ := fileHash(exe)
		return sum
	})
	h.service = &systemdService{unit: filepath.Join(root, "openbear-mesh.service"), systemctl: f.run}
	h.validate = func([]byte) error { return nil }
	h.applyWait, h.readyWait, h.exitWait, h.poll = 2*time.Second, 2*time.Second, 100*time.Millisecond, 20*time.Millisecond
	return h, f, exe
}

func meshAgent(h *meshHost, caps Capabilities) *Agent {
	a := NewAgent(Config{Capabilities: caps}, log.New(io.Discard, "", 0))
	a.meshHost = h
	return a
}

func meshDispatch(t *testing.T, a *Agent, method, params string) (map[string]any, error) {
	t.Helper()
	result, _, err := a.dispatch(context.Background(), envelope{Method: method, Params: json.RawMessage(params)})
	if err != nil {
		return nil, err
	}
	data, _ := json.Marshal(result)
	var m map[string]any
	json.Unmarshal(data, &m)
	return m, nil
}

func writeStatus(t *testing.T, h *meshHost, status map[string]any) {
	t.Helper()
	data, _ := json.Marshal(status)
	if err := replaceFile(h.path("status.json"), strings.NewReader(string(data)), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestMeshMethodsCheckCapabilityAndParams(t *testing.T) {
	h, _, _ := testMesh(t)
	if _, err := meshDispatch(t, meshAgent(h, Capabilities{}), "mesh.status", ""); err == nil || err.Error() != "capability_disabled" {
		t.Fatalf("mesh needs command execution: %v", err)
	}
	a := meshAgent(h, Capabilities{Exec: true})
	for _, c := range []struct{ method, params string }{
		{"mesh.status", `{"x":1}`},
		{"mesh.install", `[]`},
		{"mesh.apply", `{}`},
		{"mesh.apply", `{"config":[1]}`},
		{"mesh.apply", `{"config":{"schema":1}}`},
		{"mesh.apply", `{"config":{"revision":0}}`},
		{"mesh.apply", `{"config":{"revision":1.5}}`},
		{"mesh.tls", `{"activate":5}`},
		{"mesh.tls", `{"activate":""}`},
		{"mesh.tls", `{"id":"x"}`},
	} {
		if _, err := meshDispatch(t, a, c.method, c.params); err == nil || err.Error() != "invalid_params" {
			t.Errorf("%s %s: want invalid_params, got %v", c.method, c.params, err)
		}
	}
	for _, method := range []string{"mesh.apply", "mesh.tls", "mesh.upgrade"} {
		params := map[string]string{"mesh.apply": `{"config":{"revision":1}}`}[method]
		if _, err := meshDispatch(t, a, method, params); err == nil || err.Error() != "mesh_not_installed" {
			t.Errorf("%s before install: %v", method, err)
		}
	}
	status, err := meshDispatch(t, a, "mesh.status", "null")
	if err != nil || status["installed"] != false || status["service"] != "absent" || status["status"] != nil {
		t.Fatalf("status before install: %v %v", status, err)
	}
}

func TestMeshInstallIsIdempotent(t *testing.T) {
	h, f, exe := testMesh(t)
	a := meshAgent(h, Capabilities{Exec: true})
	first, err := meshDispatch(t, a, "mesh.install", "{}")
	if err != nil {
		t.Fatal(err)
	}
	if first["platform"] != "linux" || first["dataVersion"] != Version || len(first["publicKey"].(string)) != 44 || first["publicKey"] == first["controlKey"] {
		t.Fatalf("unexpected install result %v", first)
	}
	for _, name := range []string{"wg.key", "control.key"} {
		info, err := os.Stat(h.path("keys", name))
		if err != nil || info.Mode().Perm() != 0o600 {
			t.Fatalf("%s: %v %v", name, info, err)
		}
	}
	if copied, _ := os.ReadFile(h.binary()); string(copied) != "build 1" {
		t.Fatalf("binary not copied: %q", copied)
	}
	unit, _ := os.ReadFile(h.service.(*systemdService).unit)
	for _, line := range []string{"ExecStart=" + h.binary() + " mesh run --dir " + h.dir + "\n", "ConditionPathExists=" + h.path("config.json") + "\n",
		"Restart=always\n", "WantedBy=multi-user.target\n"} {
		if !strings.Contains(string(unit), line) {
			t.Fatalf("unit lacks %q:\n%s", line, unit)
		}
	}
	// A newer node binary is mesh.upgrade's business, not a second install's.
	os.WriteFile(exe, []byte("build 2"), 0o755)
	second, err := meshDispatch(t, a, "mesh.install", "")
	if err != nil || second["publicKey"] != first["publicKey"] || second["controlKey"] != first["controlKey"] {
		t.Fatalf("keys must survive a second install: %v %v", second, err)
	}
	if copied, _ := os.ReadFile(h.binary()); string(copied) != "build 1" {
		t.Fatalf("a second install replaced the copy: %q", copied)
	}
	if f.called("daemon-reload") != 1 || f.called("enable openbear-mesh") != 2 || f.called("start") != 0 {
		t.Fatalf("unexpected systemctl calls %v", f.calls)
	}
	// dataBuild is the copy's build, not the newer node binary's.
	copyBuild, _ := fileHash(h.binary())
	status, _ := meshDispatch(t, a, "mesh.status", "")
	if status["installed"] != true || status["service"] != "stopped" || status["publicKey"] != first["publicKey"] || status["dataVersion"] != Version ||
		status["dataBuild"] != copyBuild || copyBuild == h.build() {
		t.Fatalf("status after install: %v", status)
	}
	entries, _ := os.ReadDir(h.dir)
	for _, e := range entries {
		if strings.Contains(e.Name(), ".tmp-") {
			t.Fatalf("temporary file left behind: %s", e.Name())
		}
	}
}

func TestMeshLocalPrefixesSkipLoopbackAndMesh(t *testing.T) {
	cidr := func(s string) net.Addr {
		ip, n, err := net.ParseCIDR(s)
		if err != nil {
			t.Fatal(err)
		}
		n.IP = ip
		return n
	}
	ifaces := []meshIface{
		{name: "lo", flags: net.FlagUp | net.FlagLoopback, addrs: []net.Addr{cidr("127.0.0.1/8"), cidr("::1/128")}},
		{name: "eth0", flags: net.FlagUp, addrs: []net.Addr{cidr("172.17.5.9/16"), cidr("10.20.20.7/24"), cidr("fe80::1/64")}},
		{name: "eth1", flags: net.FlagUp, addrs: []net.Addr{cidr("10.20.20.8/24"), &net.IPAddr{IP: net.ParseIP("192.168.9.9")}}},
		{name: "br0", flags: net.FlagUp, addrs: []net.Addr{&net.IPNet{IP: net.ParseIP("10.20.0.1"), Mask: net.CIDRMask(112, 128)}}},
		{name: "obmesh0", flags: net.FlagUp, addrs: []net.Addr{cidr("100.96.0.2/16"), cidr("100.96.255.2/24")}},
		{name: "utun3", flags: net.FlagUp, addrs: []net.Addr{cidr("100.96.0.3/32")}},
		{name: "utun4", flags: net.FlagUp, addrs: []net.Addr{cidr("10.8.0.2/24")}},
	}
	mesh := []netip.Prefix{netip.MustParsePrefix("100.96.0.0/16"), netip.MustParsePrefix("100.96.255.0/24")}
	got := meshLocalPrefixes(ifaces, mesh)
	want := []string{"10.8.0.0/24", "10.20.0.0/16", "10.20.20.0/24", "172.17.0.0/16"}
	if !slices.Equal(got, want) {
		t.Fatalf("got %v, want %v", got, want)
	}
	// Without a config the mesh's utun cannot be told apart from other tunnels.
	if got = meshLocalPrefixes(ifaces, nil); !slices.Contains(got, "100.96.0.3/32") || slices.Contains(got, "100.96.0.0/16") {
		t.Fatalf("without a config: %v", got)
	}
	if got = meshLocalPrefixes(nil, nil); got == nil || len(got) != 0 {
		t.Fatalf("no interfaces must give an empty list: %#v", got)
	}

	h, _, _ := testMesh(t)
	a := meshAgent(h, Capabilities{Exec: true})
	os.MkdirAll(h.dir, 0o700)
	os.WriteFile(h.path("config.json"), []byte(`{"network":{"prefix":"100.96.1.9/16","controlPrefix":"100.96.255.0/24"}}`), 0o600)
	if got := h.meshPrefixes(); !slices.Equal(got, mesh) {
		t.Fatalf("mesh prefixes from config: %v", got)
	}
	for _, method := range []string{"mesh.install", "mesh.status"} {
		r, err := meshDispatch(t, a, method, "")
		list, ok := r["localPrefixes"].([]any)
		if err != nil || !ok {
			t.Fatalf("%s lacks localPrefixes: %v %v", method, r, err)
		}
		for _, s := range list {
			p, err := netip.ParsePrefix(s.(string))
			if err != nil || !p.Addr().Is4() || p != p.Masked() || p.Addr().IsLoopback() {
				t.Fatalf("%s: %q is not an IPv4 network", method, s)
			}
		}
	}
}

func TestMeshLocalAddresses(t *testing.T) {
	cidr := func(s string) net.Addr {
		ip, n, err := net.ParseCIDR(s)
		if err != nil {
			t.Fatal(err)
		}
		n.IP = ip
		return n
	}
	ifaces := []meshIface{
		{name: "lo", flags: net.FlagUp | net.FlagLoopback, addrs: []net.Addr{cidr("127.0.0.1/8")}},
		{name: "eth0", flags: net.FlagUp, addrs: []net.Addr{cidr("10.20.20.7/24"), cidr("203.0.113.5/24"), cidr("169.254.3.3/16"), cidr("fd00::7/64")}},
		{name: "eth1", flags: net.FlagUp, addrs: []net.Addr{cidr("192.168.9.9/24"), &net.IPAddr{IP: net.ParseIP("192.168.8.8")}, cidr("172.20.1.2/16")}},
		{name: "docker0", flags: net.FlagUp, addrs: []net.Addr{cidr("172.17.0.1/16")}},
		{name: "br-3f2a", flags: net.FlagUp, addrs: []net.Addr{cidr("172.18.0.1/16")}},
		{name: "bridge100", flags: net.FlagUp, addrs: []net.Addr{cidr("192.168.64.1/24")}},
		{name: "obmesh0", flags: net.FlagUp, addrs: []net.Addr{cidr("100.96.0.2/16"), cidr("10.99.0.1/24")}},
		{name: "utun3", flags: net.FlagUp, addrs: []net.Addr{cidr("100.96.0.3/32"), cidr("10.98.0.1/24")}},
		{name: "utun4", flags: net.FlagUp, addrs: []net.Addr{cidr("10.8.0.2/24")}},
	}
	mesh := []netip.Prefix{netip.MustParsePrefix("100.96.0.0/16")}
	got := meshLocalAddresses(ifaces, mesh)
	want := []string{"10.8.0.2/24", "10.20.20.7/24", "172.20.1.2/16", "192.168.9.9/24"}
	if !slices.Equal(got, want) {
		t.Fatalf("got %v, want %v", got, want)
	}
	// The networks still count every interface but the mesh's.
	if p := meshLocalPrefixes(ifaces, mesh); !slices.Contains(p, "172.17.0.0/16") || !slices.Contains(p, "10.20.20.0/24") || slices.Contains(p, "10.98.0.0/24") {
		t.Fatalf("prefixes %v", p)
	}
	if got = meshLocalAddresses(nil, nil); got == nil || len(got) != 0 {
		t.Fatalf("no interfaces must give an empty list: %#v", got)
	}

	h, _, _ := testMesh(t)
	a := meshAgent(h, Capabilities{Exec: true})
	for _, method := range []string{"mesh.install", "mesh.status"} {
		r, err := meshDispatch(t, a, method, "")
		list, ok := r["localAddresses"].([]any)
		if err != nil || !ok {
			t.Fatalf("%s lacks localAddresses: %v %v", method, r, err)
		}
		for _, s := range list {
			p, err := netip.ParsePrefix(s.(string))
			if err != nil || !p.Addr().Is4() || !p.Addr().IsPrivate() {
				t.Fatalf("%s: %q is not a private IPv4 address", method, s)
			}
		}
	}
}

func TestMeshUnitQuotesPaths(t *testing.T) {
	unit := meshUnit("/opt/my mesh/bin/openbear-mesh", "/opt/my mesh/100%")
	if !strings.Contains(unit, `ExecStart="/opt/my mesh/bin/openbear-mesh" mesh run --dir "/opt/my mesh/100%%"`+"\n") ||
		!strings.Contains(unit, "ConditionPathExists=/opt/my mesh/100%%/config.json\n") {
		t.Fatalf("paths not escaped:\n%s", unit)
	}
}

func TestMeshApplyValidatesStartsAndWaits(t *testing.T) {
	h, f, _ := testMesh(t)
	a := meshAgent(h, Capabilities{Exec: true})
	if _, err := meshDispatch(t, a, "mesh.install", ""); err != nil {
		t.Fatal(err)
	}
	h.validate = func([]byte) error { return errors.New("self.address outside network.prefix") }
	if _, err := meshDispatch(t, a, "mesh.apply", `{"config":{"schema":1,"revision":3}}`); err == nil || err.Error() != "invalid_config: self.address outside network.prefix" {
		t.Fatalf("a rejected config must not be written: %v", err)
	}
	if _, err := os.Stat(h.path("config.json")); !os.IsNotExist(err) {
		t.Fatalf("config written despite validation: %v", err)
	}
	h.validate = func([]byte) error { return nil }

	// The previous revision's error is still in status.json until the data plane reads the new config.
	writeStatus(t, h, map[string]any{"configRevision": 2, "appliedRevision": 1, "error": "port 51999 in use"})
	f.onStart = func() {
		time.Sleep(200 * time.Millisecond)
		writeStatus(t, h, map[string]any{"configRevision": 3, "appliedRevision": 3, "ready": true, "error": ""})
	}
	config := `{"schema":1,"revision":3,"self":{"name":"Berlin"}}`
	result, err := meshDispatch(t, a, "mesh.apply", `{"config":`+config+`}`)
	if err != nil || result["appliedRevision"] != 3.0 || result["ready"] != true || result["error"] != "" {
		t.Fatalf("apply: %v %v", result, err)
	}
	if written, _ := os.ReadFile(h.path("config.json")); string(written) != config {
		t.Fatalf("config not written as given: %s", written)
	}
	if f.called("start") != 1 {
		t.Fatalf("a stopped service must be started: %v", f.calls)
	}

	writeStatus(t, h, map[string]any{"configRevision": 4, "appliedRevision": 3, "error": "relay r2 has no endpoint"})
	result, err = meshDispatch(t, a, "mesh.apply", `{"config":{"revision":4}}`)
	if err != nil || result["appliedRevision"] != 3.0 || result["error"] != "relay r2 has no endpoint" {
		t.Fatalf("a failed apply must report the data plane's error: %v %v", result, err)
	}
	if f.called("start") != 1 {
		t.Fatalf("a running service must not be started again: %v", f.calls)
	}

	h.applyWait = 300 * time.Millisecond
	start := time.Now()
	result, err = meshDispatch(t, a, "mesh.apply", `{"config":{"revision":5}}`)
	if err != nil || result["appliedRevision"] != 3.0 || !strings.HasPrefix(result["error"].(string), "apply_timeout") || time.Since(start) > 2*time.Second {
		t.Fatalf("apply must give up after its wait: %v %v", result, err)
	}
}

// The controller retries a failed revision by sending it again: status.json
// still holds that revision's error, from the write before, and apply waits
// for the data plane's answer to this write.
func TestMeshApplyRetryWaitsForTheRewriteToBeRead(t *testing.T) {
	h, f, _ := testMesh(t)
	a := meshAgent(h, Capabilities{Exec: true})
	if _, err := meshDispatch(t, a, "mesh.install", ""); err != nil {
		t.Fatal(err)
	}
	f.active = true
	config := `{"schema":1,"revision":4}`
	os.WriteFile(h.path("config.json"), []byte(config), 0o600)
	old, _ := os.Stat(h.path("config.json"))
	writeStatus(t, h, map[string]any{"configRevision": 4, "appliedRevision": 3, "configStamp": old.ModTime().UnixNano(), "error": "revision 4 not applied: port 52000 in use"})
	// The data plane reads the rewrite and applies it this time.
	go func() {
		for deadline := time.Now().Add(2 * time.Second); time.Now().Before(deadline); time.Sleep(10 * time.Millisecond) {
			if info, err := os.Stat(h.path("config.json")); err == nil && !info.ModTime().Equal(old.ModTime()) {
				time.Sleep(100 * time.Millisecond)
				writeStatus(t, h, map[string]any{"configRevision": 4, "appliedRevision": 4, "configStamp": info.ModTime().UnixNano(), "ready": true, "error": ""})
				return
			}
		}
	}()
	time.Sleep(20 * time.Millisecond) // a later modification time on every file system
	result, err := meshDispatch(t, a, "mesh.apply", `{"config":`+config+`}`)
	if err != nil || result["appliedRevision"] != 4.0 || result["error"] != "" {
		t.Fatalf("the retry took the error of the write before: %v %v", result, err)
	}
	// It failed again: that error, from this write, is the answer.
	go func() {
		time.Sleep(100 * time.Millisecond)
		info, _ := os.Stat(h.path("config.json"))
		writeStatus(t, h, map[string]any{"configRevision": 5, "appliedRevision": 4, "configStamp": info.ModTime().UnixNano(), "error": "revision 5 not applied: port 52001 in use"})
	}()
	result, err = meshDispatch(t, a, "mesh.apply", `{"config":{"schema":1,"revision":5}}`)
	if err != nil || result["appliedRevision"] != 4.0 || result["error"] != "revision 5 not applied: port 52001 in use" {
		t.Fatalf("a failed apply: %v %v", result, err)
	}
}

func TestMeshTLSActivationRewritesTheConfig(t *testing.T) {
	h, _, _ := testMesh(t)
	a := meshAgent(h, Capabilities{Exec: true})
	if _, err := meshDispatch(t, a, "mesh.install", ""); err != nil {
		t.Fatal(err)
	}
	r, err := meshDispatch(t, a, "mesh.tls", "{}")
	if err != nil || r["pending"] != nil {
		t.Fatalf("tls: %v %v", r, err)
	}
	active := r["active"].(map[string]any)
	if len(active["fingerprint"].(string)) != 64 || active["notAfter"].(float64) <= active["notBefore"].(float64) {
		t.Fatalf("unexpected certificate %v", active)
	}
	config := h.path("config.json")
	os.WriteFile(config, []byte(`{"revision":2}`), 0o600)
	old := time.Now().Add(-time.Hour)
	os.Chtimes(config, old, old)
	if _, err = meshDispatch(t, a, "mesh.tls", `{"activate":"nope"}`); err == nil || !strings.HasPrefix(err.Error(), "certificate_failed") {
		t.Fatalf("an unknown certificate must be refused: %v", err)
	}
	r, err = meshDispatch(t, a, "mesh.tls", `{"activate":"`+active["id"].(string)+`"}`)
	if err != nil || r["active"].(map[string]any)["id"] != active["id"] {
		t.Fatalf("activate: %v %v", r, err)
	}
	info, _ := os.Stat(config)
	if data, _ := os.ReadFile(config); string(data) != `{"revision":2}` || !info.ModTime().After(old.Add(time.Minute)) {
		t.Fatalf("config must be rewritten unchanged: %s %v", data, info.ModTime())
	}
}

func TestMeshUpgradeReplacesTheCopyOrRollsBack(t *testing.T) {
	h, f, exe := testMesh(t)
	a := meshAgent(h, Capabilities{Exec: true})
	if _, err := meshDispatch(t, a, "mesh.install", ""); err != nil {
		t.Fatal(err)
	}
	if r, err := meshDispatch(t, a, "mesh.upgrade", ""); err != nil || r["changed"] != false {
		t.Fatalf("same build must not upgrade: %v %v", r, err)
	}
	os.WriteFile(h.path("config.json"), []byte(`{"revision":1}`), 0o600)
	f.active = true
	writeStatus(t, h, map[string]any{"pid": 999990, "ready": true})

	os.WriteFile(exe, []byte("build 2"), 0o755)
	f.onStart = func() {} // the new copy never reports
	if _, err := meshDispatch(t, a, "mesh.upgrade", ""); err == nil || err.Error() != "upgrade_not_ready" {
		t.Fatalf("an upgrade that never gets ready must fail: %v", err)
	}
	if copied, _ := os.ReadFile(h.binary()); string(copied) != "build 1" || !f.active {
		t.Fatalf("the old copy must be back and running: %q %v", copied, f.calls)
	}

	f.onStart = func() { writeStatus(t, h, map[string]any{"pid": 999991, "ready": true}) }
	r, err := meshDispatch(t, a, "mesh.upgrade", "")
	if err != nil || r["changed"] != true || r["to"] != Version {
		t.Fatalf("upgrade: %v %v", r, err)
	}
	if copied, _ := os.ReadFile(h.binary()); string(copied) != "build 2" {
		t.Fatalf("copy not replaced: %q", copied)
	}
	if _, err := os.Stat(siblingPath(h.binary(), "old")); !os.IsNotExist(err) {
		t.Fatalf("old copy left behind: %v", err)
	}
}

func TestMeshUninstallRemovesServiceAndDirectory(t *testing.T) {
	h, f, _ := testMesh(t)
	a := meshAgent(h, Capabilities{Exec: true})
	if _, err := meshDispatch(t, a, "mesh.install", ""); err != nil {
		t.Fatal(err)
	}
	r, err := meshDispatch(t, a, "mesh.uninstall", "")
	if err != nil || r["removed"] != true {
		t.Fatalf("uninstall: %v %v", r, err)
	}
	if _, err := os.Stat(h.dir); !os.IsNotExist(err) {
		t.Fatalf("mesh directory left: %v", err)
	}
	if _, err := os.Stat(h.service.(*systemdService).unit); !os.IsNotExist(err) || f.called("disable openbear-mesh") != 1 {
		t.Fatalf("unit left: %v %v", err, f.calls)
	}
	if r, err = meshDispatch(t, a, "mesh.uninstall", ""); err != nil || r["removed"] != true {
		t.Fatalf("a second uninstall must succeed: %v %v", r, err)
	}
}

func TestMeshReportCarriesOnlyNewEvents(t *testing.T) {
	h, _, _ := testMesh(t)
	a := meshAgent(h, Capabilities{Exec: true})
	report := func() (map[string]any, func()) {
		r := map[string]any{"t": "report"}
		sent := a.addMeshReport(r)
		data, _ := json.Marshal(r)
		var out map[string]any
		json.Unmarshal(data, &out)
		m, _ := out["mesh"].(map[string]any)
		return m, sent
	}
	seqs := func(m map[string]any) []float64 {
		var out []float64
		for _, e := range m["events"].([]any) {
			out = append(out, e.(map[string]any)["seq"].(float64))
		}
		return out
	}
	event := func(seq int) map[string]any {
		return map[string]any{"seq": seq, "at": 1, "kind": "peer.up", "detail": ""}
	}

	os.MkdirAll(h.dir, 0o700)
	writeStatus(t, h, map[string]any{"appliedRevision": 3, "events": []any{event(1)}})
	if m, _ := report(); m != nil {
		t.Fatalf("no mesh field before install: %v", m)
	}
	if _, err := meshDispatch(t, a, "mesh.install", ""); err != nil {
		t.Fatal(err)
	}
	writeStatus(t, h, map[string]any{"appliedRevision": 3, "ready": true, "events": []any{event(1), event(2)}})
	m, sent := report()
	if m["appliedRevision"] != 3.0 || m["ready"] != true || !slices.Equal(seqs(m), []float64{1, 2}) {
		t.Fatalf("first report: %v", m)
	}
	sent()
	if m, sent = report(); !slices.Equal(seqs(m), nil) || m["events"] == nil {
		t.Fatalf("nothing new must give an empty list: %v", m)
	}
	sent()
	writeStatus(t, h, map[string]any{"appliedRevision": 3, "events": []any{event(2), event(3), event(4)}})
	if m, _ = report(); !slices.Equal(seqs(m), []float64{3, 4}) {
		t.Fatalf("new events only: %v", m)
	}
	// That report was never sent, so the next one carries the same events.
	if m, sent = report(); !slices.Equal(seqs(m), []float64{3, 4}) {
		t.Fatalf("unsent events must be repeated: %v", m)
	}
	sent()
	// A restarted data plane numbers from 1 again.
	writeStatus(t, h, map[string]any{"appliedRevision": 3, "events": []any{event(1)}})
	if m, _ = report(); !slices.Equal(seqs(m), []float64{1}) {
		t.Fatalf("restarted numbering: %v", m)
	}
	os.WriteFile(h.path("status.json"), []byte("{broken"), 0o644)
	if m, _ = report(); m != nil {
		t.Fatalf("unreadable status must leave the report alone: %v", m)
	}
}

// mesh.status says which config.json schemas the data plane here reads, so
// the controller writes one it understands.
func TestMeshStatusReportsConfigSchemas(t *testing.T) {
	h, _, _ := testMesh(t)
	a := meshAgent(h, Capabilities{Exec: true})
	schemas := func() any {
		t.Helper()
		m, err := meshDispatch(t, a, "mesh.status", "")
		if err != nil {
			t.Fatal(err)
		}
		return m["configSchemas"]
	}
	own := []any{float64(mesh.ConfigSchemaMin), float64(mesh.ConfigSchema)}
	if got := schemas(); !slices.Equal(got.([]any), own) {
		t.Fatalf("not installed: %v, want this build's %v", got, own)
	}
	m, err := meshDispatch(t, a, "mesh.install", "")
	if err != nil || !slices.Equal(m["configSchemas"].([]any), own) {
		t.Fatalf("install: %v %v", m, err)
	}
	// The signing and end-to-end keys are made and their public halves
	// reported; mesh.status reports the same ones.
	signKey, _ := m["signKey"].(string)
	e2eKey, _ := m["e2eKey"].(string)
	if _, err := sign.ParsePublic(signKey); err != nil || len(e2eKey) != 44 {
		t.Fatalf("install reported keys %q %q", signKey, e2eKey)
	}
	if st, _ := meshDispatch(t, a, "mesh.status", ""); st["signKey"] != signKey || st["e2eKey"] != e2eKey {
		t.Fatalf("status reports keys %v %v", st["signKey"], st["e2eKey"])
	}
	r := h.record()
	if !slices.Equal(r["configSchemas"].([]any), own) {
		t.Fatalf("install record: %v", r)
	}
	// A data plane put in place by a build from before configSchemas.
	delete(r, "configSchemas")
	r["dataBuild"] = "older"
	h.saveRecord(r)
	if got := schemas(); !slices.Equal(got.([]any), []any{float64(1), float64(1)}) {
		t.Fatalf("older data plane: %v, want [1 1]", got)
	}
	// The running data plane's own report wins.
	writeStatus(t, h, map[string]any{"pid": 1, "configSchemas": []int{1, 2}})
	if got := schemas(); !slices.Equal(got.([]any), []any{float64(1), float64(2)}) {
		t.Fatalf("status.json says 1..2, got %v", got)
	}
}
