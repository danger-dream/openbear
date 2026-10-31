//go:build unix

package node

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"
)

func TestUpdateIsSingleFlightAndReleasesOnEveryOutcome(t *testing.T) {
	for _, outcome := range []string{"success", "checksum", "cancel"} {
		t.Run(outcome, func(t *testing.T) {
			binary := []byte("#!/bin/sh\necho 'openbear-node 9.9.9 (protocol v1)'\n")
			sum := sha256.Sum256(binary)
			raw, _ := json.Marshal(updateParams{Version: "9.9.9", Name: BinaryName(), SHA256: hex.EncodeToString(sum[:]), Size: int64(len(binary))})
			entered, release := make(chan struct{}), make(chan struct{})
			var calls atomic.Int32
			s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if calls.Add(1) == 1 {
					close(entered)
					select {
					case <-release:
					case <-r.Context().Done():
						return
					}
					if outcome == "checksum" {
						w.Write([]byte("bad"))
						return
					}
				}
				w.Write(binary)
			}))
			defer s.Close()
			defer close(release)
			exe := filepath.Join(t.TempDir(), "node")
			if err := os.WriteFile(exe, []byte("old"), 0o755); err != nil {
				t.Fatal(err)
			}
			a := NewAgent(Config{URL: s.URL}, nil)
			a.exe = exe
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			first := make(chan error, 1)
			go func() {
				_, err := a.update(ctx, raw)
				first <- err
			}()
			select {
			case <-entered:
			case <-ctx.Done():
				t.Fatal("update did not start its download")
			}
			if _, err := a.update(ctx, raw); err == nil || err.Error() != "node_busy" {
				t.Fatalf("overlapping update: %v", err)
			}
			if calls.Load() != 1 {
				t.Fatal("overlapping update reached the download")
			}
			if outcome == "cancel" {
				cancel()
			} else {
				release <- struct{}{}
			}
			var firstErr error
			select {
			case firstErr = <-first:
			case <-time.After(10 * time.Second):
				t.Fatal("first update did not finish")
			}
			if outcome == "success" && firstErr != nil || outcome != "success" && firstErr == nil {
				t.Fatalf("first update (%s): %v", outcome, firstErr)
			}
			if outcome != "success" {
				if b, _ := os.ReadFile(exe); string(b) != "old" {
					t.Fatal("failed update changed the installed binary")
				}
			}
			retry, stop := context.WithTimeout(context.Background(), 10*time.Second)
			defer stop()
			result, err := a.update(retry, raw)
			if err != nil || result["changed"] != true {
				t.Fatalf("update lock not released for retry: %v %v", result, err)
			}
			if b, _ := os.ReadFile(exe); string(b) != string(binary) {
				t.Fatal("retry did not install the complete binary")
			}
			if _, err := os.Stat(stagingPath(exe)); !os.IsNotExist(err) {
				t.Fatalf("staging file remains after update: %v", err)
			}
		})
	}
}
