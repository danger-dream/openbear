//go:build linux || darwin || windows

package node

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"math"
	"net"
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"strconv"
	"strings"
	"time"
)

const netPortsMax = 32

// portOwnersWithin is how long finding the holders of busy ports may
// take, TCP and UDP together: the controller waits 5 s for the answer.
const portOwnersWithin = 3 * time.Second

type portState struct {
	Free    bool   `json:"free"`
	Ours    bool   `json:"ours"`
	Process string `json:"process"`
	Pid     int    `json:"pid"`
}

// portOwner is a process holding a port; name and exe are "" when unknown.
type portOwner struct {
	pid       int
	name, exe string
}

// netPorts tells whether the mesh service could listen on the ports, and
// who holds those it could not.
func (a *Agent) netPorts(ctx context.Context, raw json.RawMessage) (any, error) {
	tcp, udp, err := parsePortParams(raw)
	if err != nil {
		return nil, err
	}
	h := a.meshNode()
	meshPid := 0
	if s, ok := h.readStatus(); ok {
		meshPid = s.Pid
	}
	ours := func(o portOwner) bool { return ownedByUs(o, h.binary(), meshPid) }
	ctx, cancel := context.WithTimeout(ctx, portOwnersWithin)
	defer cancel()
	out := checkPorts(ctx, map[string][]int{"tcp": tcp, "udp": udp}, ours)
	return map[string]any{"tcp": out["tcp"], "udp": out["udp"]}, nil
}

func parsePortParams(raw json.RawMessage) (tcp, udp []int, err error) {
	var in struct {
		TCP []float64 `json:"tcp"`
		UDP []float64 `json:"udp"`
	}
	invalid := errors.New("invalid_params")
	if !bytes.HasPrefix(bytes.TrimSpace(raw), []byte("{")) || json.Unmarshal(raw, &in) != nil || len(in.TCP)+len(in.UDP) > netPortsMax {
		return nil, nil, invalid
	}
	ports := func(list []float64) ([]int, bool) {
		out := []int{}
		for _, f := range list {
			if f != math.Trunc(f) || f < 1 || f > 65535 {
				return nil, false
			}
			out = append(out, int(f))
		}
		slices.Sort(out)
		return slices.Compact(out), true
	}
	tcp, okT := ports(in.TCP)
	udp, okU := ports(in.UDP)
	if !okT || !okU {
		return nil, nil, invalid
	}
	return tcp, udp, nil
}

// checkPorts answers for the ports of each network ("tcp", "udp"); the
// holders of busy ones are looked for once, for both networks, within
// ctx.
func checkPorts(ctx context.Context, ports map[string][]int, ours func(portOwner) bool) map[string]map[string]portState {
	out := map[string]map[string]portState{}
	busy, free := map[string][]int{}, map[string][]int{}
	for network, list := range ports {
		out[network] = map[string]portState{}
		for _, p := range list {
			ok := portFree(network, p)
			out[network][strconv.Itoa(p)] = portState{Free: ok}
			if !ok {
				busy[network] = append(busy[network], p)
			} else if bindsBesideListeners {
				free[network] = append(free[network], p)
			}
		}
	}
	// A wildcard bind succeeds there beside a listener on one address (127.0.0.1:8080): the
	// socket table says whether one is.
	if len(free) > 0 {
		for network, held := range portOwners(ctx, free) {
			for p, owners := range held {
				if len(owners) > 0 {
					out[network][strconv.Itoa(p)] = portState{}
					busy[network] = append(busy[network], p)
				}
			}
		}
	}
	if len(busy) == 0 {
		return out
	}
	owners := portOwners(ctx, busy)
	for network, list := range busy {
		for _, p := range list {
			held := owners[network][p]
			if len(held) == 0 {
				continue
			}
			// The port is ours only if nothing else holds a part of it; the
			// one reported is then the first stranger.
			slices.SortFunc(held, func(a, b portOwner) int { return a.pid - b.pid })
			pick, mine := held[0], true
			for _, o := range held {
				if !ours(o) {
					pick, mine = o, false
					break
				}
			}
			out[network][strconv.Itoa(p)] = portState{Ours: mine, Process: pick.name, Pid: pick.pid}
		}
	}
	return out
}

// ownersEach finds the holders one network at a time.
func ownersEach(busy map[string][]int, owners func(network string, ports []int) map[int][]portOwner) map[string]map[int][]portOwner {
	out := map[string]map[int][]portOwner{}
	for network, ports := range busy {
		out[network] = owners(network, ports)
	}
	return out
}

// portFree binds the port the way the mesh listeners do: [::] dual-stack,
// preceded by a plain IPv4 bind, which catches an IPv4-only holder that
// Windows (and the BSDs) would let a dual-stack bind sit beside. Both are
// closed at once.
func portFree(network string, port int) bool {
	addr4 := net.JoinHostPort("0.0.0.0", strconv.Itoa(port))
	addr := net.JoinHostPort("", strconv.Itoa(port))
	listen := func(network, addr string) (io.Closer, error) {
		if strings.HasPrefix(network, "tcp") {
			return net.Listen(network, addr)
		}
		return net.ListenPacket(network, addr)
	}
	c, err := listen(network+"4", addr4)
	if err == nil {
		c.Close()
	} else if bindRefused(err) {
		return false
	}
	// A host without IPv4 leaves the answer to this bind.
	if c, err = listen(network, addr); err != nil {
		return false
	}
	c.Close()
	return true
}

func ownedByUs(o portOwner, meshBinary string, meshPid int) bool {
	if o.pid == os.Getpid() {
		return true
	}
	if o.exe == "" {
		return meshPid > 0 && o.pid == meshPid
	}
	if samePath(o.exe, meshBinary) {
		return true
	}
	a, err1 := os.Stat(o.exe)
	b, err2 := os.Stat(meshBinary)
	return err1 == nil && err2 == nil && os.SameFile(a, b)
}

func samePath(a, b string) bool {
	a, b = filepath.Clean(a), filepath.Clean(b)
	if runtime.GOOS == "windows" {
		return strings.EqualFold(a, b)
	}
	return a == b
}

// parseLsof reads lsof -F pcPn, by network ("tcp", "udp") and port: a p
// line per process, its c line, then per socket an optional f line, its
// P line (TCP, UDP) and its n line ("*:52000", "[::1]:52000",
// "a:1->b:2"). A TCP socket with a peer is a connection, which an accept
// leaves on the port it listens on: only those without one count.
func parseLsof(data []byte) map[string]map[int][]portOwner {
	out := map[string]map[int][]portOwner{}
	var cur portOwner
	var proto string
	type key struct {
		network   string
		port, pid int
	}
	seen := map[key]bool{}
	s := bufio.NewScanner(bytes.NewReader(data))
	for s.Scan() {
		line := s.Text()
		if line == "" {
			continue
		}
		switch line[0] {
		case 'p':
			pid, _ := strconv.Atoi(line[1:])
			cur, proto = portOwner{pid: pid}, ""
		case 'c':
			cur.name = line[1:]
		case 'f':
			proto = ""
		case 'P':
			proto = strings.ToLower(line[1:])
		case 'n':
			local, _, connected := strings.Cut(line[1:], "->")
			i := strings.LastIndexByte(local, ':')
			port, err := strconv.Atoi(local[i+1:])
			k := key{proto, port, cur.pid}
			if i < 0 || err != nil || cur.pid == 0 || proto != "tcp" && proto != "udp" || proto == "tcp" && connected || seen[k] {
				continue
			}
			seen[k] = true
			if out[proto] == nil {
				out[proto] = map[int][]portOwner{}
			}
			out[proto][port] = append(out[proto][port], cur)
		}
	}
	return out
}
