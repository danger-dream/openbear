//go:build unix

package node

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func wsURL(s *httptest.Server) string { return "ws" + strings.TrimPrefix(s.URL, "http") }

func TestWebSocketFramesPingAndClose(t *testing.T) {
	big := strings.Repeat("x", 70000)
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := accept(w, r)
		if err != nil {
			return
		}
		c.writeFrame(opPing, []byte("hb"))
		op, data, err := c.Read()
		if err != nil || op != opText || string(data) != big {
			t.Errorf("server read %v %d", err, len(data))
		}
		// A fragmented message must be reassembled by the client.
		c.wmu.Lock()
		c.raw.Write([]byte{0x01, 3, 'a', 'b', 'c'})
		c.raw.Write([]byte{0x80, 2, 'd', 'e'})
		c.wmu.Unlock()
		c.CloseWith(closeRevoked, "revoked")
	}))
	defer s.Close()
	c, err := Dial(context.Background(), wsURL(s)+"/x?y=1", nil, 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if err = c.WriteText([]byte(big)); err != nil {
		t.Fatal(err)
	}
	op, data, err := c.Read()
	if err != nil || op != opText || string(data) != "abcde" {
		t.Fatalf("fragmented read: %v %q", err, data)
	}
	_, _, err = c.Read()
	var closeErr *CloseError
	if !errors.As(err, &closeErr) || closeErr.Code != closeRevoked {
		t.Fatalf("close code not propagated: %v", err)
	}
}

func TestHandshakeRejection(t *testing.T) {
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusUnauthorized) }))
	defer s.Close()
	_, err := Dial(context.Background(), wsURL(s), nil, 5*time.Second)
	var h *HandshakeError
	if !errors.As(err, &h) || h.Status != http.StatusUnauthorized {
		t.Fatalf("expected 401 handshake error, got %v", err)
	}
}

func TestEncryptedSessionReportsExecutesAndIsRevoked(t *testing.T) {
	f := newFake()
	replies := make(chan map[string]any, 1)
	s := f.serve(t, func(ch *channel, auth map[string]any) {
		if auth["version"] != Version || auth["protocol"].(float64) != ProtocolVersion {
			t.Errorf("auth lacks version: %v", auth)
		}
		if hello := next(t, ch, "hello"); hello["info"].(map[string]any)["arch"] == "" {
			t.Errorf("bad hello %v", hello)
		}
		report := next(t, ch, "report")["m"].(map[string]any)
		if report["mem"].(map[string]any)["total"].(float64) <= 0 || report["nodeRss"].(float64) <= 0 {
			t.Errorf("bad report %v", report)
		}
		replies <- call(t, ch, "exec", map[string]any{"command": "printf hi; echo err >&2; exit 3"})
		revoke(ch)
	})
	defer s.Close()
	if err := runSession(t, f.agent(s.URL, Capabilities{Exec: true})); !errors.Is(err, errRevoked) {
		t.Fatalf("expected revocation, got %v", err)
	}
	reply := <-replies
	result := reply["result"].(map[string]any)
	if reply["ok"] != true || result["stdout"] != "hi" || result["stderr"] != "err\n" || result["exitCode"].(float64) != 3 {
		t.Fatalf("unexpected exec reply %v", reply)
	}
}

func TestNodeRefusesAControllerWithAnotherIdentity(t *testing.T) {
	f := newFake()
	_, impostor, _ := ed25519.GenerateKey(rand.Reader)
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if ch, _ := f.handshake(t, w, r, impostor); ch != nil {
			ch.conn.Read() // the node hangs up before anything else
		}
	}))
	defer s.Close()
	if err := runSession(t, f.agent(s.URL, Capabilities{Exec: true})); !errors.Is(err, errControllerIdentity) {
		t.Fatalf("an impostor controller must be refused, got %v", err)
	}
}

func TestTamperedFrameEndsTheSession(t *testing.T) {
	f := newFake()
	s := f.serve(t, func(ch *channel, _ map[string]any) {
		next(t, ch, "hello")
		ch.mu.Lock()
		sealed := ch.tx.Seal(nil, counterNonce(ch.sent), append([]byte{kindJSON}, `{"t":"call","id":"x","method":"exec","params":{"command":"touch /tmp/never"}}`...), nil)
		sealed[len(sealed)-1] ^= 1
		ch.conn.WriteBinary(sealed)
		ch.mu.Unlock()
		ch.conn.Read()
	})
	defer s.Close()
	if err := runSession(t, f.agent(s.URL, Capabilities{Exec: true})); !errors.Is(err, errBadFrame) {
		t.Fatalf("a tampered frame must end the session, got %v", err)
	}
}

func TestCapabilitiesAreEnforcedLocally(t *testing.T) {
	f := newFake()
	got := map[string]map[string]any{}
	s := f.serve(t, func(ch *channel, _ map[string]any) {
		for _, method := range []string{"exec", "fs.read", "fs.edit", "job.start", "job.list", "update", "ping"} {
			got[method] = call(t, ch, method, map[string]any{"command": "true", "path": "/etc/hostname"})
		}
		ch.conn.CloseWith(closeRevoked, "")
	})
	defer s.Close()
	runSession(t, f.agent(s.URL, Capabilities{}))
	for _, method := range []string{"exec", "fs.read", "fs.edit", "job.start", "job.list", "update"} {
		if got[method]["ok"] != false || got[method]["error"] != "capability_disabled" {
			t.Fatalf("%s must be refused locally: %v", method, got[method])
		}
	}
	if got["ping"]["ok"] != true {
		t.Fatalf("ping should work: %v", got["ping"])
	}
}

func TestExecTimeoutKillsProcessGroup(t *testing.T) {
	start := time.Now()
	r, err := Exec(context.Background(), json.RawMessage(`{"command":"sleep 30 & sleep 30","timeout":1}`))
	if err != nil || !r.TimedOut || time.Since(start) > 5*time.Second {
		t.Fatalf("timeout not enforced: %+v %v", r, err)
	}
}

// Output past the limit comes back as its first and last parts with the
// omission marked between them, not as a middle piece.
func TestExecLongOutputKeepsStartAndEnd(t *testing.T) {
	r, err := Exec(context.Background(), json.RawMessage(`{"command":"seq 1 300000; seq 1 300000 >&2","timeout":30}`))
	if err != nil || r.ExitCode != 0 || !r.Truncated {
		t.Fatalf("seq: %v %v", err, r.Truncated)
	}
	for name, out := range map[string]string{"stdout": r.Stdout, "stderr": r.Stderr} {
		head, tail, _ := splitCapped(t, out)
		if !strings.HasPrefix(head, "1\n2\n3\n") || !strings.HasSuffix(tail, "\n299999\n300000\n") {
			t.Fatalf("%s: starts %q, ends %q", name, head[:min(len(head), 20)], tail[max(0, len(tail)-20):])
		}
	}
}

func TestControllerURLPolicy(t *testing.T) {
	for raw, ok := range map[string]bool{"https://a.example": true, "http://10.0.0.1:18961": true, "http://127.0.0.1:1": true,
		"http://100.96.0.1:18961": true, "http://100.127.255.1": true, "http://100.128.0.1": false, "http://100.63.255.1": false,
		"http://192.0.2.1": false, "http://169.254.169.254": false, "http://a.example": false, "http://8.8.8.8": false, "ftp://x": false, "https://u@a.example": false} {
		if _, err := ControllerURL(raw); (err == nil) != ok {
			t.Errorf("%s: %v", raw, err)
		}
	}
}

func TestFilesListReadWriteAndChunkedUpload(t *testing.T) {
	dir := t.TempDir()
	u := newUploads()
	enc := base64.StdEncoding.EncodeToString
	path := filepath.Join(dir, "conf", "app.conf")
	if _, err := fileCall(t, u, "fs.write", map[string]any{"path": path, "data": enc([]byte("v1"))}); err == nil || err.Error() != "not_found" {
		t.Fatalf("missing parent without mkdirs: %v", err)
	}
	if _, err := fileCall(t, u, "fs.write", map[string]any{"path": path, "data": enc([]byte("v1")), "mkdirs": true}); err != nil {
		t.Fatal(err)
	}
	os.Chmod(path, 0o600)
	// An overwrite keeps the existing mode and goes through a symlink.
	link := filepath.Join(dir, "link.conf")
	os.Symlink(path, link)
	if _, err := fileCall(t, u, "fs.write", map[string]any{"path": link, "data": enc([]byte("version two"))}); err != nil {
		t.Fatal(err)
	}
	if b, _ := os.ReadFile(path); string(b) != "version two" {
		t.Fatalf("write did not reach the link target: %q", b)
	}
	if st, _ := os.Lstat(link); st.Mode()&os.ModeSymlink == 0 {
		t.Fatal("the symlink itself was replaced")
	}
	if st, _ := os.Stat(path); st.Mode().Perm() != 0o600 {
		t.Fatalf("mode not preserved: %v", st.Mode())
	}
	r, err := fileCall(t, u, "fs.read", map[string]any{"path": path, "offset": 8, "limit": 10})
	if err != nil || r["eof"] != true || r["size"].(float64) != 11 {
		t.Fatalf("read tail: %v %v", r, err)
	}
	if data, _ := base64.StdEncoding.DecodeString(r["data"].(string)); string(data) != "two" {
		t.Fatalf("read tail data %q", data)
	}
	big := filepath.Join(dir, "big.bin")
	for i, chunk := range []string{"aaa", "bbb", "cc"} {
		_, err := fileCall(t, u, "fs.write", map[string]any{"path": big, "upload": "upload0001", "offset": i * 3, "data": enc([]byte(chunk)), "done": i == 2, "mode": "0750"})
		if err != nil {
			t.Fatalf("chunk %d: %v", i, err)
		}
		if _, statErr := os.Stat(big); i < 2 && statErr == nil {
			t.Fatal("a partial upload must not appear at the target")
		}
	}
	if b, _ := os.ReadFile(big); string(b) != "aaabbbcc" {
		t.Fatalf("chunked upload: %q", b)
	}
	if st, _ := os.Stat(big); st.Mode().Perm() != 0o750 {
		t.Fatalf("requested mode not applied: %v", st.Mode())
	}
	fileCall(t, u, "fs.write", map[string]any{"path": big, "upload": "upload0002", "offset": 0, "data": enc([]byte("x"))})
	if _, err := fileCall(t, u, "fs.write", map[string]any{"path": big, "upload": "upload0002", "offset": 5, "data": enc([]byte("y"))}); err == nil || err.Error() != "upload_out_of_order" {
		t.Fatalf("a gap must be refused: %v", err)
	}
	listing, err := fileCall(t, u, "fs.list", map[string]any{"path": dir})
	if err != nil {
		t.Fatal(err)
	}
	entries := listing["entries"].([]any)
	first := entries[0].(map[string]any)
	if first["name"] != "conf" || first["type"] != "dir" {
		t.Fatalf("directories come first: %v", entries)
	}
	var sawLink bool
	for _, e := range entries {
		e := e.(map[string]any)
		if e["name"] == "link.conf" {
			sawLink = e["type"] == "link" && e["target"] == path
		}
		if strings.Contains(e["name"].(string), ".obtmp-") {
			t.Fatalf("temp file left behind: %v", e)
		}
	}
	if !sawLink {
		t.Fatalf("symlink not described: %v", entries)
	}
	for method, p := range map[string]map[string]any{"fs.read": {"path": dir}, "fs.stat": {"path": filepath.Join(dir, "nope")}, "fs.list": {"path": "relative"}} {
		if _, err := fileCall(t, u, method, p); err == nil {
			t.Fatalf("%s %v should fail", method, p)
		}
	}
}

// updateServer serves a stand-in binary next to the connect endpoint.
func updateServer(t *testing.T, f *fakeController, binary []byte, fn func(ch *channel)) *httptest.Server {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/server-nodes/v1/download/"+BinaryName(), func(w http.ResponseWriter, r *http.Request) { w.Write(binary) })
	mux.HandleFunc("/api/server-nodes/v1/connect", func(w http.ResponseWriter, r *http.Request) {
		if ch, _ := f.handshake(t, w, r, nil); ch != nil {
			fn(ch)
		}
	})
	return httptest.NewServer(mux)
}

func TestUpdateReplacesTheBinaryThenAsksForRestart(t *testing.T) {
	f := newFake()
	binary := []byte("#!/bin/sh\necho 'openbear-node 9.9.9 (protocol v1)'\n")
	sum := sha256.Sum256(binary)
	params := map[string]any{"version": "9.9.9", "name": BinaryName(), "sha256": hex.EncodeToString(sum[:]), "size": len(binary)}
	replies := make(chan map[string]any, 2)
	s := updateServer(t, f, binary, func(ch *channel) {
		wrong := map[string]any{}
		for k, v := range params {
			wrong[k] = v
		}
		wrong["sha256"] = strings.Repeat("0", 64)
		replies <- call(t, ch, "update", wrong)
		replies <- call(t, ch, "update", params)
		ch.conn.Read()
	})
	defer s.Close()
	exe := filepath.Join(t.TempDir(), "openbear-node")
	os.WriteFile(exe, []byte("old"), 0o755)
	a := f.agent(s.URL, Capabilities{Exec: true})
	a.exe = exe
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := a.Run(ctx); !errors.Is(err, ErrRestart) {
		t.Fatalf("expected a restart request, got %v", err)
	}
	bad, good := <-replies, <-replies
	if bad["ok"] != false || bad["error"] != "checksum_mismatch" {
		t.Fatalf("a wrong checksum must be refused: %v", bad)
	}
	if good["ok"] != true || good["result"].(map[string]any)["to"] != "9.9.9" {
		t.Fatalf("update reply: %v", good)
	}
	if b, _ := os.ReadFile(exe); string(b) != string(binary) {
		t.Fatalf("binary not replaced: %q", b)
	}
	if _, err := os.Stat(stagingPath(exe)); err == nil {
		t.Fatal("staging file left behind")
	}
}

func TestUpdateDownloadsThroughTheAddressInUse(t *testing.T) {
	f := newFake()
	binary := []byte("#!/bin/sh\necho 'openbear-node 9.9.9 (protocol v1)'\n")
	sum := sha256.Sum256(binary)
	replies := make(chan map[string]any, 1)
	s := updateServer(t, f, binary, func(ch *channel) {
		replies <- call(t, ch, "update", map[string]any{"version": "9.9.9", "name": BinaryName(), "sha256": hex.EncodeToString(sum[:]), "size": len(binary)})
		ch.conn.Read()
	})
	defer s.Close()
	exe := filepath.Join(t.TempDir(), "openbear-node")
	os.WriteFile(exe, []byte("old"), 0o755)
	// Connected through the backup because the primary is down.
	a := f.agent("http://127.0.0.1:1", Capabilities{Exec: true})
	a.Config.Backup, a.backup, a.exe = s.URL, true, exe
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := a.Run(ctx); !errors.Is(err, ErrRestart) {
		t.Fatalf("expected a restart request, got %v (%v)", err, <-replies)
	}
	if reply := <-replies; reply["ok"] != true {
		t.Fatalf("update through the backup: %v", reply)
	}
}

func TestUninstallRequestEndsRunAfterReplying(t *testing.T) {
	f := newFake()
	replies := make(chan map[string]any, 1)
	s := f.serve(t, func(ch *channel, _ map[string]any) {
		replies <- call(t, ch, "uninstall", map[string]any{})
		ch.conn.Read()
	})
	defer s.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := f.agent(s.URL, Capabilities{}).Run(ctx); !errors.Is(err, ErrUninstalled) {
		t.Fatalf("expected uninstall, got %v", err)
	}
	if reply := <-replies; reply["ok"] != true {
		t.Fatalf("reply: %v", reply)
	}
	path := filepath.Join(t.TempDir(), "etc", "config.json")
	SaveConfig(path, f.config(s.URL, Capabilities{}))
	if err := Uninstall(path, false); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(path); err == nil {
		t.Fatal("config not removed")
	}
}

func TestURLChangeIsProvedBeforeItIsSaved(t *testing.T) {
	f := newFake()
	var probes, sessions atomic.Int32
	moved := f.serve(t, func(ch *channel, auth map[string]any) {
		if auth["probe"] == true {
			probes.Add(1)
			return
		}
		next(t, ch, "hello")
		sessions.Add(1)
		ch.conn.CloseWith(closeRevoked, "")
	})
	defer moved.Close()
	replies := make(chan map[string]any, 2)
	old := f.serve(t, func(ch *channel, _ map[string]any) {
		replies <- call(t, ch, "config.url", map[string]any{"url": "http://127.0.0.1:1"})
		replies <- call(t, ch, "config.url", map[string]any{"url": moved.URL})
		ch.conn.Read()
	})
	defer old.Close()
	path := filepath.Join(t.TempDir(), "config.json")
	a := f.agent(old.URL, Capabilities{})
	a.ConfigPath = path
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	go func() {
		for sessions.Load() == 0 && ctx.Err() == nil {
			time.Sleep(20 * time.Millisecond)
		}
		cancel()
	}()
	a.Run(ctx)
	bad, good := <-replies, <-replies
	if bad["ok"] != false || bad["error"] != "new_url_unreachable" {
		t.Fatalf("an unreachable address must not be saved: %v", bad)
	}
	if good["ok"] != true || probes.Load() != 1 || sessions.Load() != 1 {
		t.Fatalf("switch: %v probes=%d sessions=%d", good, probes.Load(), sessions.Load())
	}
	if saved, err := LoadConfig(path); err != nil || saved.URL != moved.URL {
		t.Fatalf("new address not saved: %v %v", saved.URL, err)
	}
}

func TestBackupAddressTakesOverAndPrimaryWinsBack(t *testing.T) {
	fastSwitch(t, 300*time.Millisecond, 100*time.Millisecond)
	f := newFake()
	homeProbeInterval = 300 * time.Millisecond
	defer func() { homeProbeInterval = 10 * time.Minute }()
	hellos := make(chan map[string]any, 4)
	session := func(ch *channel, auth map[string]any) {
		if auth["probe"] == true {
			return
		}
		hellos <- next(t, ch, "hello")
		ch.conn.Read() // stays connected until the node leaves
	}
	backup := f.serve(t, session)
	defer backup.Close()
	// The primary's port is reserved but refuses connections until it is started.
	primary := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if ch, auth := f.handshake(t, w, r, nil); ch != nil {
			session(ch, auth)
		}
	}))
	hostPort := primary.Listener.Addr().String()
	primary.Listener.Close()
	address := "http://" + hostPort
	a := f.agent(address, Capabilities{})
	a.Config.Backup = backup.URL
	ctx, cancel := context.WithTimeout(context.Background(), 40*time.Second)
	defer cancel()
	runUntilDone(t, ctx, cancel, a)
	first := <-hellos
	if first["backup"] != true || first["endpoint"] != backup.URL {
		t.Fatalf("expected the backup to take over: %v", first)
	}
	listener, err := net.Listen("tcp", hostPort)
	if err != nil {
		t.Fatal(err)
	}
	primary.Listener = listener
	primary.Start()
	defer primary.Close()
	select {
	case home := <-hellos:
		if home["backup"] != false || home["endpoint"] != address {
			t.Fatalf("expected to move back to the primary: %v", home)
		}
	case <-ctx.Done():
		t.Fatal("never returned to the primary")
	}
}

// From the 10-30 check: moving back to the primary ended the session at once, killing a command
// running on the backup. It moves once the command is done, and the reply still arrives.
func TestMovingBackToThePrimaryWaitsForRunningCalls(t *testing.T) {
	fastSwitch(t, 300*time.Millisecond, 100*time.Millisecond)
	f := newFake()
	homeProbeInterval, idlePoll = 300*time.Millisecond, 50*time.Millisecond
	defer func() { homeProbeInterval, idlePoll = 10*time.Minute, time.Second }()
	marker := filepath.Join(t.TempDir(), "finished")
	replies := make(chan string, 1)
	hellos := make(chan map[string]any, 4)
	backup := f.serve(t, func(ch *channel, auth map[string]any) {
		if auth["probe"] == true {
			return
		}
		hellos <- next(t, ch, "hello")
		ch.send(map[string]any{"t": "call", "id": "exec-1", "method": "exec",
			"params": map[string]any{"command": "sleep 2; touch " + marker + "; echo done", "timeout": 30}})
		for {
			k, data, err := ch.read()
			if err != nil {
				return
			}
			if k == kindJSON && strings.Contains(string(data), `"reply"`) {
				replies <- string(data)
			}
		}
	})
	defer backup.Close()
	primary := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if ch, auth := f.handshake(t, w, r, nil); ch != nil && auth["probe"] != true {
			hellos <- next(t, ch, "hello")
			ch.conn.Read()
		}
	}))
	hostPort := primary.Listener.Addr().String()
	primary.Listener.Close()
	a := f.agent("http://"+hostPort, Capabilities{Exec: true})
	a.Config.Backup = backup.URL
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	runUntilDone(t, ctx, cancel, a)
	if first := <-hellos; first["backup"] != true {
		t.Fatalf("expected the backup first: %v", first)
	}
	listener, err := net.Listen("tcp", hostPort)
	if err != nil {
		t.Fatal(err)
	}
	primary.Listener = listener
	primary.Start()
	defer primary.Close()
	select {
	case r := <-replies:
		if !strings.Contains(r, "done") {
			t.Fatalf("exec reply: %s", r)
		}
	case <-ctx.Done():
		t.Fatal("the exec reply never arrived")
	}
	if _, err := os.Stat(marker); err != nil {
		t.Fatal("the command did not run to its end")
	}
	select {
	case home := <-hellos:
		if home["backup"] != false {
			t.Fatalf("expected to move back to the primary: %v", home)
		}
	case <-ctx.Done():
		t.Fatal("never returned to the primary")
	}
}
