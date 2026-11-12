//go:build !linux && !darwin && !windows

package node

import (
	"context"
	"errors"
)

// Other platforms list no processes until their collectors exist.
var topSorts = []string{}

func protectedPID(int) bool { return true }

func signalProcess(int, bool) (bool, error) { return false, errors.New("unsupported_method") }

func processAlive(int) bool { return false }

func hostTop(context.Context, string, int, bool) (HostTop, error) {
	return HostTop{}, errors.New("unsupported_method")
}

func hostDetails(_ context.Context, info Info) (HostDetails, error) {
	return HostDetails{Hostname: info.Hostname, OS: info.OS, Kernel: info.Kernel, Arch: info.Arch, CPU: HostCPU{Model: info.CPU, Threads: info.Cores},
		Interfaces: interfaces(nil), Disks: []HostDisk{}, Mounts: []HostMount{}}, nil
}
