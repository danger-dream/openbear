package node

import (
	"bytes"
	"context"
	"errors"
	"os/exec"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"syscall"
	"time"

	"golang.org/x/sys/unix"
)

func bindRefused(err error) bool {
	return errors.Is(err, syscall.EADDRINUSE) || errors.Is(err, syscall.EACCES)
}

// portOwners asks lsof, which macOS ships, once for both networks: the
// socket tables are only reachable through libproc there. lsof stops
// with ctx.
// bindsBesideListeners: a wildcard bind is let through beside a listener on one address
// (SO_REUSEADDR on the BSDs, the default on Windows).
const bindsBesideListeners = true

func portOwners(ctx context.Context, busy map[string][]int) map[string]map[int][]portOwner {
	out := map[string]map[int][]portOwner{}
	for network, owners := range parseLsof(runLsof(ctx, lsofArgs(busy))) {
		out[network] = map[int][]portOwner{}
		for port, list := range owners {
			if !slices.Contains(busy[network], port) {
				continue
			}
			for _, o := range list {
				if o.exe = processPath(o.pid); o.exe != "" {
					o.name = filepath.Base(o.exe)
				}
				out[network][port] = append(out[network][port], o)
			}
		}
	}
	return out
}

// lsofArgs selects the sockets on the ports, an -i for each network,
// which lsof lists together. No -sTCP:LISTEN: a state list for TCP drops
// the UDP sockets; parseLsof leaves out TCP connections instead.
func lsofArgs(busy map[string][]int) []string {
	args := []string{"-nP", "+c", "0", "-F", "pcPn"}
	for _, network := range []string{"tcp", "udp"} {
		if len(busy[network]) == 0 {
			continue
		}
		list := make([]string, len(busy[network]))
		for i, p := range busy[network] {
			list[i] = strconv.Itoa(p)
		}
		args = append(args, "-i"+strings.ToUpper(network)+":"+strings.Join(list, ","))
	}
	return args
}

func runLsof(ctx context.Context, args []string) []byte {
	lsof := "/usr/sbin/lsof"
	if p, err := exec.LookPath("lsof"); err == nil {
		lsof = p
	}
	cmd := exec.CommandContext(ctx, lsof, args...)
	// lsof forks helpers, which may keep its output open past a kill.
	cmd.WaitDelay = 500 * time.Millisecond
	// lsof exits 1 when nothing matched; whatever it printed still counts.
	data, _ := cmd.Output()
	return data
}

// processPath reads the executable path from kern.procargs2: argc, then
// the path the process was started from.
func processPath(pid int) string {
	b, err := unix.SysctlRaw("kern.procargs2", pid)
	if err != nil || len(b) < 5 {
		return ""
	}
	path, _, _ := bytes.Cut(b[4:], []byte{0})
	if !filepath.IsAbs(string(path)) {
		return ""
	}
	return string(path)
}
