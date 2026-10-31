//go:build linux || darwin || windows

package node

import (
	"errors"
	"os"
	"testing"
)

type removalService struct {
	meshService
	current string
	after   string
	err     error
}

func (s *removalService) state() string { return s.current }
func (s *removalService) stop() error {
	if s.after != "" {
		s.current = s.after
	}
	return s.err
}

func TestRemovalRequiresExitNotJustSuccessfulStop(t *testing.T) {
	if waitExit(os.Getpid(), 0) {
		t.Fatal("live process reported exited")
	}
	s := &removalService{current: "running", after: "stopped"}
	if stopForRemoval(s, os.Getpid(), 0) == nil {
		t.Fatal("successful stop with live process accepted")
	}
	s = &removalService{current: "running", after: "stopped", err: errors.New("already stopped")}
	if err := stopForRemoval(s, 0, 0); err != nil {
		t.Fatal("stopped process refused because of stop result", err)
	}
	s = &removalService{current: "absent"}
	for range 2 {
		if err := stopForRemoval(s, 0, 0); err != nil {
			t.Fatal("repeat removal refused", err)
		}
	}
	s = &removalService{current: "running", err: errors.New("stop failed")}
	if stopForRemoval(s, 0, 0) == nil {
		t.Fatal("service still running accepted")
	}
}
