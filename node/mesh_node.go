//go:build linux || darwin || windows

package node

import (
	"bytes"
	"context"
	"crypto/ecdh"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"

	"openbear/node/mesh"
	"openbear/node/mesh/sign"
	"openbear/node/mesh/transport"
)

// The mesh data plane runs as its own service from a copy of this binary in
// the mesh directory, so updating the node never restarts it and, on Windows,
// never holds the file a node update renames.
//
//	bin/openbear-mesh  the copy the service runs
//	config.json        written by mesh.apply, read by the data plane
//	applied.json       the data plane's copy of the config it last applied
//	status.json        written by the data plane every second
//	keys/              private keys (wg.key, control.key, e2e.key: WireGuard;
//	                   sign.key: Ed25519); only the public halves leave
//	install.json       what mesh.install did, for upgrade and uninstall

// meshDirFor follows jobsDir: an installed Linux node keeps it under /var/lib.
func meshDirFor(config string) string {
	if config == DefaultConfigPath && runtime.GOOS == "linux" {
		return "/var/lib/openbear-node/mesh"
	}
	return filepath.Join(filepath.Dir(config), "mesh")
}

// meshService is the system service that runs the data plane.
type meshService interface {
	// install registers bin to run with dir; it starts only once dir holds a config.
	install(bin, dir string) error
	state() string // "absent", "stopped" or "running"
	start() error
	// stop returns once the service manager reports it stopped.
	stop() error
	remove() error
}

type meshHost struct {
	mu       sync.Mutex // install, apply, upgrade and uninstall run one at a time
	dir      string
	service  meshService
	exe      func() (string, error)
	build    func() string
	validate func([]byte) error

	applyWait, readyWait, exitWait, poll time.Duration
	changeTimer                          *time.Timer

	seqMu   sync.Mutex
	sentSeq int64 // newest event in a report that was sent

	routeMu sync.Mutex // the two below, for the control connection (mesh_route.go)
	applied appliedPrefix
	iface   string // the data plane's interface, once found
}

type meshStatus struct {
	Pid             int    `json:"pid"`
	ConfigRevision  int64  `json:"configRevision"`
	AppliedRevision int64  `json:"appliedRevision"`
	ConfigStamp     int64  `json:"configStamp"`
	Error           string `json:"error"`
	Ready           bool   `json:"ready"`
}

func newMeshHost(dir string, exe func() (string, error), build func() string) *meshHost {
	return &meshHost{dir: dir, service: newMeshService(dir), exe: exe, build: build, validate: mesh.Validate,
		applyWait: 15 * time.Second, readyWait: 30 * time.Second, exitWait: 20 * time.Second, poll: 200 * time.Millisecond}
}

func (a *Agent) meshNode() *meshHost {
	a.meshOnce.Do(func() {
		if a.meshHost != nil {
			return
		}
		dir := filepath.Join(os.TempDir(), "openbear-node-mesh")
		if a.ConfigPath != "" {
			dir = meshDirFor(a.ConfigPath)
		}
		a.meshHost = newMeshHost(dir, a.executable, a.build)
	})
	return a.meshHost
}

func (a *Agent) meshCall(ctx context.Context, method string, raw json.RawMessage) (any, error) {
	h := a.meshNode()
	switch method {
	case "mesh.prepare", "mesh.activate", "mesh.confirm", "mesh.abort":
		return a.meshChangeCall(method, raw)
	}
	if what := h.takesDown(method, raw); what != "" {
		var release func()
		ctx, release = a.leaveMesh(ctx, what)
		defer release()
	}
	switch method {
	case "mesh.apply":
		return h.apply(ctx, raw)
	case "mesh.tls":
		return h.tls(raw)
	}
	if !emptyParams(raw) {
		return nil, errors.New("invalid_params")
	}
	switch method {
	case "mesh.status":
		return h.status(), nil
	case "mesh.install":
		return h.install()
	case "mesh.upgrade":
		return h.upgrade(ctx)
	case "mesh.uninstall":
		if err := h.uninstall(); err != nil {
			return nil, err
		}
		return map[string]any{"removed": true}, nil
	}
	return nil, errors.New("unsupported_method")
}

// emptyParams accepts no params, null or {}.
func emptyParams(raw json.RawMessage) bool {
	var p map[string]json.RawMessage
	return len(bytes.TrimSpace(raw)) == 0 || json.Unmarshal(raw, &p) == nil && len(p) == 0
}

func (h *meshHost) path(name ...string) string {
	return filepath.Join(append([]string{h.dir}, name...)...)
}

func (h *meshHost) binary() string {
	name := "openbear-mesh"
	if runtime.GOOS == "windows" {
		name += ".exe"
	}
	return h.path("bin", name)
}

// record is install.json; nil until an install finished.
func (h *meshHost) record() map[string]any {
	data, err := os.ReadFile(h.path("install.json"))
	if err != nil {
		return nil
	}
	var r map[string]any
	if json.Unmarshal(data, &r) != nil || r == nil {
		return nil
	}
	return r
}

func (h *meshHost) saveRecord(r map[string]any) error {
	data, _ := json.MarshalIndent(r, "", "  ")
	return replaceFile(h.path("install.json"), bytes.NewReader(append(data, '\n')), 0o600)
}

func (h *meshHost) readStatus() (meshStatus, bool) {
	var s meshStatus
	data, err := os.ReadFile(h.path("status.json"))
	return s, err == nil && json.Unmarshal(data, &s) == nil
}

func (h *meshHost) status() map[string]any {
	r := h.record()
	version, _ := r["dataVersion"].(string)
	dataBuild, _ := r["dataBuild"].(string)
	if r != nil {
		// An install from before the signing and end-to-end keys gets them
		// here, so the controller learns them before the data plane that
		// needs them is put in place.
		h.ensureSecurityKeys()
	}
	out := map[string]any{"installed": r != nil, "service": h.service.state(), "dataVersion": version, "dataBuild": dataBuild,
		"publicKey": publicKeyOf(h.path("keys", "wg.key")), "controlKey": publicKeyOf(h.path("keys", "control.key")),
		"signKey": signPublicOf(h.path("keys", "sign.key")), "e2eKey": publicKeyOf(h.path("keys", "e2e.key")),
		"localPrefixes": h.localPrefixes(), "localAddresses": h.localAddresses(), "status": nil}
	data, err := os.ReadFile(h.path("status.json"))
	if err == nil && json.Valid(data) {
		out["status"] = json.RawMessage(data)
	} else {
		data = nil
	}
	out["configSchemas"] = h.configSchemas(r, data)
	out["change"] = h.changeView()
	return out
}

// configSchemas is [min, max] of the config.json schemas the data plane
// here reads, so the controller knows which to write: what the running data
// plane reports, else what was recorded when its binary was put in place,
// else [1, 1] for one from before either. Without an install it is this
// build's, the data plane an install puts in place.
func (h *meshHost) configSchemas(r map[string]any, status []byte) [2]int {
	own := mesh.ConfigSchemas()
	if r == nil {
		return own
	}
	valid := func(s []int) bool { return len(s) == 2 && 1 <= s[0] && s[0] <= s[1] }
	var s struct {
		ConfigSchemas []int `json:"configSchemas"`
	}
	if status != nil && json.Unmarshal(status, &s) == nil && valid(s.ConfigSchemas) {
		return [2]int(s.ConfigSchemas)
	}
	if raw, ok := r["configSchemas"].([]any); ok && len(raw) == 2 {
		low, _ := raw[0].(float64)
		high, _ := raw[1].(float64)
		if rec := []int{int(low), int(high)}; valid(rec) {
			return [2]int(rec)
		}
	}
	if build, _ := r["dataBuild"].(string); build != "" && build == h.build() {
		return own
	}
	return [2]int{1, 1}
}

func (h *meshHost) install() (any, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if err := h.allowMeshMutation(); err != nil {
		return nil, err
	}
	keys := h.path("keys")
	for _, dir := range []string{h.dir, h.path("bin"), keys} {
		if err := os.MkdirAll(dir, 0o700); err != nil {
			return nil, fileError(err)
		}
	}
	if err := protectDir(h.dir); err != nil {
		return nil, fileError(err)
	}
	if err := protectDir(keys); err != nil {
		return nil, fileError(err)
	}
	public, err := ensureMeshKey(filepath.Join(keys, "wg.key"))
	if err != nil {
		return nil, fileError(err)
	}
	control, err := ensureMeshKey(filepath.Join(keys, "control.key"))
	if err != nil {
		return nil, fileError(err)
	}
	signKey, e2eKey, err := h.ensureSecurityKeys()
	if err != nil {
		return nil, fileError(err)
	}
	r := h.record()
	if r == nil {
		r = map[string]any{"schema": 1, "installedAt": time.Now().UnixMilli()}
	}
	bin := h.binary()
	if _, err := os.Stat(bin); errors.Is(err, os.ErrNotExist) {
		exe, err := h.exe()
		if err != nil {
			return nil, errors.New("executable_unknown")
		}
		if err = copyExecutable(exe, bin); err != nil {
			return nil, fileError(err)
		}
		r["dataVersion"], r["dataBuild"], r["configSchemas"] = Version, h.build(), mesh.ConfigSchemas()
	} else if err != nil {
		return nil, fileError(err)
	} else if _, ok := r["dataBuild"]; !ok {
		// A copy from an install that stopped before writing its record.
		if sum, _ := fileHash(bin); sum != "" && sum == h.build() {
			r["dataVersion"], r["dataBuild"], r["configSchemas"] = Version, sum, mesh.ConfigSchemas()
		}
	}
	r["platform"] = runtime.GOOS
	if _, ok := r["wintun"]; !ok {
		r["wintun"] = meshInstallsWintun()
	}
	if err = h.service.install(bin, h.dir); err != nil {
		return nil, errors.New("service_failed: " + err.Error())
	}
	if err = h.saveRecord(r); err != nil {
		return nil, fileError(err)
	}
	version, _ := r["dataVersion"].(string)
	return map[string]any{"publicKey": public, "controlKey": control, "signKey": signKey, "e2eKey": e2eKey, "platform": runtime.GOOS, "dataVersion": version,
		"localPrefixes": h.localPrefixes(), "localAddresses": h.localAddresses(), "configSchemas": h.configSchemas(r, nil)}, nil
}

// apply writes the controller's config as given and waits for the data plane
// to apply that revision or report why it could not.
func (h *meshHost) apply(ctx context.Context, raw json.RawMessage) (any, error) {
	var p struct {
		Config json.RawMessage `json:"config"`
	}
	var head struct {
		Revision int64 `json:"revision"`
	}
	if json.Unmarshal(raw, &p) != nil || !bytes.HasPrefix(p.Config, []byte("{")) || json.Unmarshal(p.Config, &head) != nil || head.Revision <= 0 {
		return nil, errors.New("invalid_params")
	}
	if err := h.validate(p.Config); err != nil {
		return nil, errors.New("invalid_config: " + err.Error())
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.record() == nil || h.service.state() == "absent" {
		return nil, errors.New("mesh_not_installed")
	}
	if err := h.allowMeshMutation(); err != nil {
		return nil, err
	}
	if err := replaceFile(h.path("config.json"), bytes.NewReader(p.Config), 0o600); err != nil {
		return nil, fileError(err)
	}
	var stamp int64
	if info, err := os.Stat(h.path("config.json")); err == nil {
		stamp = info.ModTime().UnixNano()
	}
	if h.service.state() != "running" {
		if err := h.service.start(); err != nil {
			return nil, errors.New("service_failed: " + err.Error())
		}
	}
	// An error counts only once the data plane has read this write; the one
	// in status.json may still be about the previous config, or about this
	// revision written before, when this is a retry. A data plane older
	// than the stamp is taken at its revision.
	s, done := h.wait(ctx, h.applyWait, func(s meshStatus) bool {
		return s.AppliedRevision == head.Revision || s.ConfigRevision == head.Revision && s.Error != "" && (s.ConfigStamp == 0 || s.ConfigStamp == stamp)
	})
	result := map[string]any{"appliedRevision": s.AppliedRevision, "ready": s.Ready, "error": s.Error}
	if !done {
		result["error"] = "apply_timeout"
		if s.Error != "" {
			result["error"] = "apply_timeout: " + s.Error
		}
	}
	return result, nil
}

// tls reports the relay's TLS certificate and the pending successor, if any.
// Activating one rewrites the same config.json so the data plane reloads the
// certificate.
func (h *meshHost) tls(raw json.RawMessage) (any, error) {
	var p struct {
		Activate string `json:"activate"`
	}
	if !emptyParams(raw) {
		d := json.NewDecoder(bytes.NewReader(raw))
		d.DisallowUnknownFields()
		if d.Decode(&p) != nil || p.Activate == "" {
			return nil, errors.New("invalid_params")
		}
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.record() == nil {
		return nil, errors.New("mesh_not_installed")
	}
	dir := h.path("tls")
	if p.Activate != "" {
		if err := h.allowMeshMutation(); err != nil {
			return nil, err
		}
		if err := transport.ActivateCertificate(dir, p.Activate); err != nil {
			return nil, errors.New("certificate_failed: " + err.Error())
		}
		if config, err := os.ReadFile(h.path("config.json")); err == nil {
			if err = replaceFile(h.path("config.json"), bytes.NewReader(config), 0o600); err != nil {
				return nil, fileError(err)
			}
		}
	}
	certs, err := transport.EnsureCertificate(dir)
	if err != nil {
		return nil, errors.New("certificate_failed: " + err.Error())
	}
	out := map[string]any{"active": certificateView(certs.Active), "pending": nil}
	if certs.Pending != nil {
		out["pending"] = certificateView(*certs.Pending)
	}
	return out, nil
}

func certificateView(c transport.Certificate) map[string]any {
	return map[string]any{"id": c.ID, "fingerprint": c.FingerprintSHA256, "notBefore": c.NotBefore.UnixMilli(), "notAfter": c.NotAfter.UnixMilli()}
}

// upgrade replaces the data plane's copy when this node's build differs from
// it, and goes back to the old copy if the new one does not come up.
func (h *meshHost) upgrade(ctx context.Context) (any, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if err := h.allowMeshMutation(); err != nil {
		return nil, err
	}
	r := h.record()
	if r == nil {
		return nil, errors.New("mesh_not_installed")
	}
	from, _ := r["dataVersion"].(string)
	build := h.build()
	exe, err := h.exe()
	if err != nil || build == "" {
		return nil, errors.New("executable_unknown")
	}
	bin := h.binary()
	if current, _ := fileHash(bin); current == build {
		return map[string]any{"from": from, "to": from, "changed": false}, nil
	}
	// This build's data plane signs and encrypts end to end with keys an
	// older install does not have.
	signKey, e2eKey, err := h.ensureSecurityKeys()
	if err != nil {
		return nil, fileError(err)
	}
	// A config.json in a schema this build does not read (from the release
	// before a protocol change) leaves the new data plane waiting for the
	// controller's next config, not ready: that is the upgrade working.
	readable := configReadable(h.path("config.json"))
	staged, old := siblingPath(bin, "new"), siblingPath(bin, "old")
	if err = copyExecutable(exe, staged); err != nil {
		os.Remove(staged)
		return nil, fileError(err)
	}
	before, _ := h.readStatus()
	running := h.service.state() == "running"
	if running {
		if err = h.service.stop(); err != nil {
			os.Remove(staged)
			return nil, errors.New("service_failed: " + err.Error())
		}
		waitExit(before.Pid, h.exitWait)
	}
	os.Remove(old)
	if err = os.Rename(bin, old); err != nil && !errors.Is(err, os.ErrNotExist) {
		os.Remove(staged)
		h.restart(running)
		return nil, fileError(err)
	}
	if err = os.Rename(staged, bin); err != nil {
		os.Rename(old, bin)
		h.restart(running)
		return nil, fileError(err)
	}
	if _, err = os.Stat(h.path("config.json")); err == nil {
		wasReady := running && before.Ready && readable
		err = h.service.start()
		var s meshStatus
		ok := false
		if err == nil {
			s, ok = h.wait(ctx, h.readyWait, func(s meshStatus) bool {
				return s.Pid > 0 && s.Pid != before.Pid && (s.Ready || !wasReady)
			})
		}
		if !ok {
			h.service.stop()
			waitExit(s.Pid, h.exitWait)
			if os.Rename(old, bin) == nil {
				h.service.start()
			}
			return nil, errors.New("upgrade_not_ready")
		}
	}
	os.Remove(old)
	r["dataVersion"], r["dataBuild"], r["configSchemas"] = Version, build, mesh.ConfigSchemas()
	if err = h.saveRecord(r); err != nil {
		return nil, fileError(err)
	}
	return map[string]any{"from": from, "to": Version, "changed": true, "configSchemas": mesh.ConfigSchemas(), "signKey": signKey, "e2eKey": e2eKey}, nil
}

// configReadable says whether this build's data plane reads the schema of
// the config in path; a missing or unreadable file counts as readable.
func configReadable(path string) bool {
	raw, err := os.ReadFile(path)
	if err != nil {
		return true
	}
	var head struct {
		Schema int `json:"schema"`
	}
	if json.Unmarshal(raw, &head) != nil {
		return true
	}
	s := mesh.ConfigSchemas()
	return s[0] <= head.Schema && head.Schema <= s[1]
}

// ensureSecurityKeys makes the signing key (keys/sign.key) and the
// end-to-end key (keys/e2e.key) when missing and returns their public halves.
func (h *meshHost) ensureSecurityKeys() (signKey, e2eKey string, err error) {
	keys := h.path("keys")
	if err = os.MkdirAll(keys, 0o700); err != nil {
		return "", "", err
	}
	if signKey, err = ensureSignKey(filepath.Join(keys, "sign.key")); err != nil {
		return "", "", err
	}
	e2eKey, err = ensureMeshKey(filepath.Join(keys, "e2e.key"))
	return signKey, e2eKey, err
}

func (h *meshHost) restart(running bool) {
	if running {
		h.service.start()
	}
}

func (h *meshHost) uninstall() error {
	h.mu.Lock()
	defer h.mu.Unlock()
	if err := h.allowMeshMutation(); err != nil {
		return err
	}
	return h.remove()
}

// remove stops and deletes the service, which takes down the interface,
// routes and firewall rules, then deletes the mesh directory.
func (h *meshHost) remove() error {
	r := h.record()
	before, _ := h.readStatus()
	if runtime.GOOS != "linux" {
		if err := stopForRemoval(h.service, before.Pid, h.exitWait); err != nil {
			return err
		}
	}
	if h.service.state() != "absent" {
		if err := h.service.remove(); err != nil {
			return errors.New("service_failed: " + err.Error())
		}
	}
	waitExit(before.Pid, h.exitWait)
	if r["wintun"] == true {
		removeWintun(h.dir)
	}
	var err error
	// Windows may still hold the copy for a moment after the service stopped.
	for range 10 {
		if err = os.RemoveAll(h.dir); err == nil {
			return nil
		}
		time.Sleep(500 * time.Millisecond)
	}
	return fileError(err)
}

// uninstallMesh removes the mesh this node installed, before the node's own files go.
func uninstallMesh(configPath string) {
	h := newMeshHost(meshDirFor(configPath), nil, nil)
	if h.record() != nil {
		h.remove()
		return
	}
	os.RemoveAll(h.dir)
}

// wait polls status.json until done accepts it, the time is up or ctx ends;
// it returns the last status read and whether done accepted it.
func (h *meshHost) wait(ctx context.Context, limit time.Duration, done func(meshStatus) bool) (meshStatus, bool) {
	deadline := time.Now().Add(limit)
	var last meshStatus
	for {
		if s, ok := h.readStatus(); ok {
			last = s
			if done(s) {
				return s, true
			}
		}
		if time.Now().After(deadline) {
			return last, false
		}
		select {
		case <-ctx.Done():
			return last, false
		case <-time.After(h.poll):
		}
	}
}

// addMeshReport adds the data plane's status to a report, carrying only the
// events newer than the last report sent; call the returned func once it was.
func (a *Agent) addMeshReport(report map[string]any) func() {
	h := a.meshNode()
	if h.record() == nil {
		return func() {}
	}
	data, err := os.ReadFile(h.path("status.json"))
	var status map[string]json.RawMessage
	if err != nil || json.Unmarshal(data, &status) != nil || status == nil {
		return func() {}
	}
	var events []json.RawMessage
	json.Unmarshal(status["events"], &events)
	seqs := make([]int64, len(events))
	newest := int64(0)
	for i, e := range events {
		var v struct {
			Seq int64 `json:"seq"`
		}
		json.Unmarshal(e, &v)
		seqs[i], newest = v.Seq, max(newest, v.Seq)
	}
	h.seqMu.Lock()
	last := h.sentSeq
	h.seqMu.Unlock()
	if len(events) > 0 && newest < last {
		last = 0 // the data plane restarted its numbering
	}
	fresh := []json.RawMessage{}
	for i, e := range events {
		if seqs[i] > last {
			fresh = append(fresh, e)
		}
	}
	status["events"], _ = json.Marshal(fresh)
	report["mesh"] = status
	sent := max(last, newest)
	if len(events) == 0 {
		sent = last
	}
	return func() {
		h.seqMu.Lock()
		h.sentSeq = sent
		h.seqMu.Unlock()
	}
}

// ensureMeshKey returns the public half of the WireGuard private key in path,
// creating the key when there is none.
func ensureMeshKey(path string) (string, error) {
	data, err := os.ReadFile(path)
	if err == nil {
		if public := meshPublicKey(string(data)); public != "" {
			return public, nil
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return "", err
	}
	key := make([]byte, 32)
	rand.Read(key)
	key[0] &= 248
	key[31] = key[31]&127 | 64
	private := base64.StdEncoding.EncodeToString(key)
	if err = replaceFile(path, strings.NewReader(private), 0o600); err != nil {
		return "", err
	}
	return meshPublicKey(private), nil
}

// ensureSignKey returns the public half of the Ed25519 key in path,
// creating the key when there is none.
func ensureSignKey(path string) (string, error) {
	data, err := os.ReadFile(path)
	if err == nil {
		if key, err := sign.ParsePrivate(data); err == nil {
			return sign.Public(key), nil
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return "", err
	}
	seed := make([]byte, 32)
	rand.Read(seed)
	private := base64.StdEncoding.EncodeToString(seed)
	if err = replaceFile(path, strings.NewReader(private), 0o600); err != nil {
		return "", err
	}
	key, err := sign.ParsePrivate([]byte(private))
	if err != nil {
		return "", err
	}
	return sign.Public(key), nil
}

func signPublicOf(path string) string {
	data, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	key, err := sign.ParsePrivate(data)
	if err != nil {
		return ""
	}
	return sign.Public(key)
}

func publicKeyOf(path string) string {
	data, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return meshPublicKey(string(data))
}

// meshPublicKey is the curve25519 public key of a base64 private key, or "".
func meshPublicKey(private string) string {
	raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(private))
	if err != nil || len(raw) != 32 {
		return ""
	}
	key, err := ecdh.X25519().NewPrivateKey(raw)
	if err != nil {
		return ""
	}
	return base64.StdEncoding.EncodeToString(key.PublicKey().Bytes())
}

// replaceFile replaces path with what r holds, so a reader sees the old file
// or the whole new one.
func replaceFile(path string, r io.Reader, perm os.FileMode) error {
	temp := filepath.Join(filepath.Dir(path), "."+filepath.Base(path)+".tmp-"+randomHex(4))
	f, err := os.OpenFile(temp, os.O_WRONLY|os.O_CREATE|os.O_EXCL, perm)
	if err != nil {
		return err
	}
	_, err = io.Copy(f, r)
	if err == nil {
		err = f.Chmod(perm)
	}
	if err == nil {
		err = f.Sync()
	}
	if closeErr := f.Close(); err == nil {
		err = closeErr
	}
	if err == nil {
		err = os.Rename(temp, path)
	}
	if err != nil {
		os.Remove(temp)
	}
	return err
}

func copyExecutable(src, dst string) error {
	f, err := os.Open(src)
	if err != nil {
		return err
	}
	defer f.Close()
	return replaceFile(dst, f, 0o755)
}

func fileHash(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	sum := sha256.New()
	if _, err = io.Copy(sum, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(sum.Sum(nil)), nil
}

// siblingPath keeps the extension last: Windows runs only files ending in .exe.
func siblingPath(path, tag string) string {
	ext := filepath.Ext(path)
	return strings.TrimSuffix(path, ext) + "." + tag + ext
}

func waitExit(pid int, limit time.Duration) bool {
	if pid <= 0 {
		return true
	}
	deadline := time.Now().Add(limit)
	for {
		if _, alive := processStart(pid); !alive {
			return true
		}
		if !time.Now().Before(deadline) {
			return false
		}
		time.Sleep(100 * time.Millisecond)
	}
}

func stopForRemoval(service meshService, pid int, limit time.Duration) error {
	var stopErr error
	if service.state() != "absent" {
		stopErr = service.stop()
	}
	if !waitExit(pid, limit) || service.state() == "running" {
		if stopErr != nil {
			return fmt.Errorf("service_failed: still running: %w", stopErr)
		}
		return errors.New("service_failed: mesh process did not exit")
	}
	return nil
}

// serviceCommand runs a service manager command; its error carries what the command printed.
func serviceCommand(name string, args ...string) (string, error) {
	var stderr bytes.Buffer
	cmd := exec.Command(name, args...)
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil && stderr.Len() > 0 {
		err = fmt.Errorf("%s %s: %s", name, strings.Join(args, " "), strings.TrimSpace(stderr.String()))
	}
	return strings.TrimSpace(string(out)), err
}
