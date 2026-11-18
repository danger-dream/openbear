package node

import (
	"errors"
	"strconv"
)

// Scoped to this node process: a reboot or node restart invalidates every old
// confirmation, even if the OS reuses both a PID and a boot-relative start tick.
var hostIdentityEpoch = randomHex(16)

func hostProcessIdentity(pid int, start string) string {
	if start == "" || start == "0" {
		return ""
	}
	return hostIdentityEpoch + ":" + strconv.Itoa(pid) + ":" + start
}

func currentProcessIdentity(pid int) string {
	start, alive := processStart(pid)
	if !alive {
		return ""
	}
	return hostProcessIdentity(pid, start)
}

func checkProcessIdentity(pid int, expected string) error {
	if expected == "" {
		return errors.New("process_identity_required")
	}
	actual := currentProcessIdentity(pid)
	if actual == "" {
		if !processAlive(pid) {
			return errors.New("process_not_found")
		}
		return errors.New("process_identity_unavailable")
	}
	if actual != expected {
		return errors.New("process_changed")
	}
	return nil
}

func processInstanceExited(pid int, expected string) bool {
	actual := currentProcessIdentity(pid)
	if actual != "" {
		return actual != expected
	}
	// Failure to inspect an existing process is not proof that it exited.
	return !processAlive(pid)
}
