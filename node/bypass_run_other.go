//go:build !linux

package node

import (
	"errors"
	"os/exec"
)

// Only Linux nodes bypass a transparent proxy.
func bypassedCommand(*exec.Cmd) {}

func RunBypassed([]string) error { return errors.New("bypass-run is for Linux") }
