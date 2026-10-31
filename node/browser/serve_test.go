package browser

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"io"
	"strings"
	"testing"
	"time"
)

type serveHarness struct {
	in   *io.PipeWriter
	out  *io.PipeReader
	done chan error
}

func startServe(t *testing.T) *serveHarness {
	t.Helper()
	inR, inW := io.Pipe()
	outR, outW := io.Pipe()
	h := &serveHarness{in: inW, out: outR, done: make(chan error, 1)}
	go func() { h.done <- Serve(context.Background(), inR, outW); outW.Close() }()
	t.Cleanup(func() { inW.Close(); outR.Close() })
	return h
}

func (h *serveHarness) send(t *testing.T, v any) {
	t.Helper()
	body, _ := json.Marshal(v)
	frame := binary.BigEndian.AppendUint32(nil, uint32(len(body)))
	if _, err := h.in.Write(append(frame, body...)); err != nil {
		t.Fatal(err)
	}
}

func (h *serveHarness) recv(t *testing.T) (map[string]any, string) {
	t.Helper()
	var header [4]byte
	if _, err := io.ReadFull(h.out, header[:]); err != nil {
		t.Fatal(err)
	}
	body := make([]byte, binary.BigEndian.Uint32(header[:]))
	if _, err := io.ReadFull(h.out, body); err != nil {
		t.Fatal(err)
	}
	var v map[string]any
	if err := json.Unmarshal(body, &v); err != nil {
		t.Fatal(err)
	}
	return v, string(body)
}

func TestServeProtocolErrorsBusyAndShutdown(t *testing.T) {
	url := cdpServer(t, func(s *serverWS, msg map[string]any) {
		if msg["method"] == "Target.attachToTarget" {
			return // bound pages hang so requests stay in flight
		}
		s.json(map[string]any{"id": msg["id"], "result": map[string]any{}})
	})
	h := startServe(t)
	h.send(t, map[string]any{"id": 1, "action": "snapshot", "params": map[string]any{}})
	if _, body := h.recv(t); body != `{"id":1,"error":"worker_not_initialized","outcome":"not_started","details":{}}` {
		t.Fatalf("not initialized: %s", body)
	}
	// Unexpected failures leak neither endpoint nor credentials.
	h.send(t, map[string]any{"id": 2, "action": "initialize", "params": map[string]any{
		"endpoint": "ws://user:secret@127.0.0.1:1/devtools/browser/x", "connectTimeoutS": 1,
		"downloadHostPath": t.TempDir(),
	}})
	if _, body := h.recv(t); body != `{"id":2,"error":"browser_operation_failed","outcome":"unknown"}` {
		t.Fatalf("connect failure: %s", body)
	}
	h.send(t, map[string]any{"id": 3, "action": "initialize", "params": map[string]any{
		"endpoint": url, "connectTimeoutS": 2, "downloadHostPath": t.TempDir(),
		"snapshotMaxChars": 1000, "maxEventEntries": 10, "maxBodyBytes": 100, "maxArtifactBytes": 1000,
	}})
	if _, body := h.recv(t); body != `{"id":3,"result":{"connected":true,"engine":"go-cdp"}}` {
		t.Fatalf("initialize: %s", body)
	}
	h.send(t, map[string]any{"id": 4, "action": "configure", "params": nil})
	if _, body := h.recv(t); body != `{"id":4,"error":"browser_operation_failed","outcome":"unknown"}` {
		t.Fatalf("bad params: %s", body)
	}
	for i := 0; i < 64; i++ {
		h.send(t, map[string]any{"id": 100 + i, "action": "snapshot", "params": map[string]any{"targetId": "T", "timeoutMs": 60000}})
	}
	h.send(t, map[string]any{"id": "busy", "action": "snapshot", "params": map[string]any{"targetId": "T"}})
	if _, body := h.recv(t); body != `{"id":"busy","error":"worker_busy","outcome":"not_started"}` {
		t.Fatalf("busy: %s", body)
	}
	// End of input stops the worker; cancelled requests are not answered.
	h.in.Close()
	select {
	case err := <-h.done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("serve did not stop")
	}
	if b, _ := io.ReadAll(h.out); len(b) != 0 {
		t.Fatalf("late replies %q", b)
	}
}

func TestServeStopsOnInvalidFrame(t *testing.T) {
	h := startServe(t)
	h.in.Write([]byte{0, 0, 0, 0})
	select {
	case <-h.done:
	case <-time.After(2 * time.Second):
		t.Fatal("zero-length frame accepted")
	}
	h2 := startServe(t)
	h2.in.Write(binary.BigEndian.AppendUint32(nil, maxFrame+1))
	select {
	case <-h2.done:
	case <-time.After(2 * time.Second):
		t.Fatal("oversized frame accepted")
	}
}

func TestReplyTooLargeAndCompactEncoding(t *testing.T) {
	body := encodeReply(O("id", 7, "result", O("value", strings.Repeat("é", maxFrame/2))))
	if string(body) != `{"id": 7, "error": "worker_response_too_large", "outcome": "unknown"}` {
		t.Fatalf("%s", body)
	}
	body = encodeReply(O("id", 8, "result", O("value", "é<>", "n", 1.5)))
	if string(body) != `{"id":8,"result":{"value":"é<>","n":1.5}}` {
		t.Fatalf("%s", body)
	}
}
