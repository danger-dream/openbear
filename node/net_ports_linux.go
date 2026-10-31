package node

import (
	"bufio"
	"context"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"syscall"
)

func bindRefused(err error) bool {
	return errors.Is(err, syscall.EADDRINUSE) || errors.Is(err, syscall.EACCES)
}

// portOwners reads /proc, which takes well within the controller's wait
// even with tens of thousands of descriptors.
// bindsBesideListeners: Linux refuses a wildcard bind beside any listener on the port.
const bindsBesideListeners = false

func portOwners(_ context.Context, busy map[string][]int) map[string]map[int][]portOwner {
	return ownersEach(busy, networkOwners)
}

// networkOwners finds the sockets on the ports in /proc/net, then the
// processes holding them through their descriptors.
func networkOwners(network string, ports []int) map[int][]portOwner {
	inodes := map[string]int{} // socket inode -> port
	for _, name := range []string{network, network + "6"} {
		readSocketTable("/proc/net/"+name, network == "tcp", ports, inodes)
	}
	out := map[int][]portOwner{}
	if len(inodes) == 0 {
		return out
	}
	procs, _ := os.ReadDir("/proc")
	for _, p := range procs {
		pid, err := strconv.Atoi(p.Name())
		if err != nil {
			continue
		}
		fds, _ := os.ReadDir(filepath.Join("/proc", p.Name(), "fd"))
		seen := map[int]bool{}
		for _, fd := range fds {
			link, err := os.Readlink(filepath.Join("/proc", p.Name(), "fd", fd.Name()))
			if !strings.HasPrefix(link, "socket:[") {
				continue
			}
			port, ok := inodes[strings.TrimSuffix(strings.TrimPrefix(link, "socket:["), "]")]
			if err != nil || !ok || seen[port] {
				continue
			}
			seen[port] = true
			out[port] = append(out[port], processOwner(pid))
		}
	}
	return out
}

// readSocketTable adds the sockets bound to the ports; for TCP only the
// listening ones, as the connections an accept leaves share the port.
func readSocketTable(path string, tcp bool, ports []int, inodes map[string]int) {
	f, err := os.Open(path)
	if err != nil {
		return
	}
	defer f.Close()
	s := bufio.NewScanner(f)
	s.Scan() // the header
	for s.Scan() {
		// sl local_address rem_address st tx_queue:rx_queue tr:tm->when retrnsmt uid timeout inode
		fields := strings.Fields(s.Text())
		if len(fields) < 10 || tcp && fields[3] != "0A" || fields[9] == "0" {
			continue
		}
		i := strings.LastIndexByte(fields[1], ':')
		port, err := strconv.ParseUint(fields[1][i+1:], 16, 16)
		if i < 0 || err != nil || !slices.Contains(ports, int(port)) {
			continue
		}
		inodes[fields[9]] = int(port)
	}
}

func processOwner(pid int) portOwner {
	o := portOwner{pid: pid}
	dir := filepath.Join("/proc", strconv.Itoa(pid))
	if comm, err := os.ReadFile(filepath.Join(dir, "comm")); err == nil {
		o.name = strings.TrimSpace(string(comm))
	}
	if exe, err := os.Readlink(filepath.Join(dir, "exe")); err == nil {
		o.exe = strings.TrimSuffix(exe, " (deleted)")
	}
	return o
}
