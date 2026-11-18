//go:build linux

package node

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestMeshRemovePreservesFilesWhenLinuxStopCannotBeConfirmed(t *testing.T) {
	for _, mode := range []string{"stop-error", "status-error", "live-pid"} {
		t.Run(mode, func(t *testing.T) {
			h, f, _ := testMesh(t)
			a := meshAgent(h, Capabilities{Exec: true})
			changeCall(t, a, "mesh.install", "{}")
			unit := h.service.(*systemdService).unit
			config := h.path("config.json")
			if err := os.WriteFile(config, []byte("recovery-material"), 0o600); err != nil {
				t.Fatal(err)
			}
			f.active = true
			h.service.(*systemdService).systemctl = func(args ...string) (string, error) {
				if mode == "stop-error" && args[0] == "stop" {
					return "", errors.New("stop denied")
				}
				if mode == "status-error" && args[0] == "is-active" {
					return "", errors.New("manager unavailable")
				}
				return f.run(args...)
			}
			if mode == "live-pid" {
				writeStatus(t, h, map[string]any{"pid": os.Getpid()})
				h.exitWait = 0
			}
			if err := h.remove(); err == nil {
				t.Fatal("unsafe removal succeeded")
			}
			if _, err := os.Stat(unit); err != nil {
				t.Fatal("unit deleted", err)
			}
			if b, err := os.ReadFile(config); err != nil || string(b) != "recovery-material" {
				t.Fatal("recovery data deleted", err)
			}
			if f.called("disable") != 0 {
				t.Fatal("service disabled before confirmed exit")
			}
		})
	}
}

func TestProcdRemovePreservesScriptOnStopOrStatusFailure(t *testing.T) {
	for _, failure := range []string{"stop", "ubus", "malformed"} {
		t.Run(failure, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "mesh-init")
			os.WriteFile(path, []byte("recovery script"), 0o755)
			disabled := false
			s := &procdService{script: path, name: "test", poll: time.Millisecond,
				run: func(name string, args ...string) (string, error) {
					if name == "ubus" {
						if failure == "malformed" {
							return "not-json", nil
						}
						return "", errors.New("ubus unavailable")
					}
					if args[0] == "disable" {
						disabled = true
					}
					if args[0] == failure {
						return "", errors.New("stop refused")
					}
					return "", nil
				}}
			if err := s.remove(); err == nil {
				t.Fatal("stop/status failure ignored")
			}
			if _, err := os.Stat(path); err != nil || disabled {
				t.Fatal("script disabled/deleted", err)
			}
		})
	}
}

func TestMeshRollbackRestoresOriginallyStoppedState(t *testing.T) {
	for _, timeout := range []bool{false, true} {
		t.Run(map[bool]string{false: "abort", true: "expiry"}[timeout], func(t *testing.T) {
			a, h := changeFixture(t)
			if err := h.service.stop(); err != nil {
				t.Fatal(err)
			}
			changeCall(t, a, "mesh.prepare", changePrepare)
			activateChange(t, a, h)
			if h.service.state() != "running" {
				t.Fatal("activation did not start service")
			}
			if timeout {
				expireChange(t, h)
				a.resumeMeshChange()
			} else {
				changeCall(t, a, "mesh.abort", `{"id":"change-1"}`)
			}
			if h.service.state() != "stopped" || h.changeView()["state"] != "aborted" {
				t.Fatal("original stop state not restored")
			}
			for _, name := range []string{"config.json", "applied.json"} {
				data, _ := os.ReadFile(h.path(name))
				if string(data) != `{"revision":1}` {
					t.Fatalf("%s not restored: %s", name, data)
				}
			}
			if a.config().URL != "http://100.96.0.1:18961" {
				t.Fatal("URL not restored")
			}
		})
	}
}

func TestStoppedRollbackDoesNotClaimAbortedOnFailedStop(t *testing.T) {
	a, h := changeFixture(t)
	h.service.stop()
	changeCall(t, a, "mesh.prepare", changePrepare)
	activateChange(t, a, h)
	s := h.service.(*systemdService)
	run := s.systemctl
	s.systemctl = func(args ...string) (string, error) {
		if args[0] == "stop" {
			return "", errors.New("stop refused")
		}
		return run(args...)
	}
	_, err := meshDispatch(t, a, "mesh.abort", `{"id":"change-1"}`)
	if err == nil || !strings.Contains(err.Error(), "stop refused") {
		t.Fatal(err)
	}
	if h.changeView()["state"] == "aborted" {
		t.Fatal("failed stop marked aborted")
	}
	s.systemctl = run
	a.resumeMeshChange()
	if h.service.state() != "stopped" || h.changeView()["state"] != "aborted" {
		t.Fatal("retry did not restore stopped service")
	}
}
