package node

import (
	"cmp"
	"context"
	"encoding/json"
	"errors"
	"net"
	"os"
	"slices"
	"strings"
	"time"
)

// HostProcess is one process as host.top lists it. CPU is its share of the
// whole machine, so the rows add up to the CPU gauge.
type HostProcess struct {
	PID       int     `json:"pid"`
	Name      string  `json:"name"`
	User      string  `json:"user,omitempty"`
	Command   string  `json:"command,omitempty"`
	CPU       float64 `json:"cpu"`
	RSS       int64   `json:"rss"`
	ReadRate  uint64  `json:"readRate,omitempty"`
	WriteRate uint64  `json:"writeRate,omitempty"`
	Conns     int     `json:"conns,omitempty"`
}

// HostRate is the traffic of one interface, or of one network namespace (a
// container) through all of its interfaces.
type HostRate struct {
	Name   string `json:"name"`
	PIDs   int    `json:"pids,omitempty"`
	RXRate uint64 `json:"rxRate"`
	TXRate uint64 `json:"txRate"`
}

type HostTop struct {
	Sort      string        `json:"sort"`
	Sorts     []string      `json:"sorts"`
	Count     int           `json:"count"`
	Processes []HostProcess `json:"processes"`
	// Linux keeps no per-process traffic; by interface and by container it does.
	Interfaces []HostRate `json:"interfaces,omitempty"`
	Netns      []HostRate `json:"netns,omitempty"`
}

type HostCPU struct {
	Model   string   `json:"model"`
	Vendor  string   `json:"vendor,omitempty"`
	Sockets int      `json:"sockets,omitempty"`
	Cores   int      `json:"cores,omitempty"`
	Threads int      `json:"threads"`
	MHz     int      `json:"mhz,omitempty"`
	MaxMHz  int      `json:"maxMhz,omitempty"`
	Cache   string   `json:"cache,omitempty"`
	Flags   []string `json:"flags,omitempty"`
}

type HostBoard struct {
	Vendor  string `json:"vendor,omitempty"`
	Product string `json:"product,omitempty"`
	Version string `json:"version,omitempty"`
	BIOS    string `json:"bios,omitempty"`
}

type HostDisk struct {
	Name  string `json:"name"`
	Model string `json:"model,omitempty"`
	Size  int64  `json:"size"`
	// Kind is ssd, hdd or nvme; empty when the system does not say.
	Kind string `json:"kind,omitempty"`
}

type HostMount struct {
	Path   string `json:"path"`
	FS     string `json:"fs"`
	Device string `json:"device,omitempty"`
	Total  int64  `json:"total"`
	Used   int64  `json:"used"`
}

type HostIface struct {
	Name  string   `json:"name"`
	MAC   string   `json:"mac,omitempty"`
	MTU   int      `json:"mtu,omitempty"`
	Up    bool     `json:"up"`
	Speed int      `json:"speed,omitempty"` // Mbit/s
	Addrs []string `json:"addrs,omitempty"`
	RX    uint64   `json:"rx,omitempty"`
	TX    uint64   `json:"tx,omitempty"`
}

type HostDetails struct {
	Hostname       string       `json:"hostname"`
	OS             string       `json:"os"`
	Kernel         string       `json:"kernel"`
	Arch           string       `json:"arch"`
	Virtualization string       `json:"virtualization,omitempty"`
	Boot           int64        `json:"boot,omitempty"`
	Timezone       string       `json:"timezone,omitempty"`
	DNS            []string     `json:"dns,omitempty"`
	CPU            HostCPU      `json:"cpu"`
	Board          HostBoard    `json:"board"`
	MemTotal       int64        `json:"memTotal"`
	SwapTotal      int64        `json:"swapTotal"`
	Disks          []HostDisk   `json:"disks"`
	Mounts         []HostMount  `json:"mounts"`
	Interfaces     []HostIface  `json:"interfaces"`
	Proxy          *ProxyStatus `json:"proxy,omitempty"`
}

// topSample is how far apart the two looks behind a CPU or rate figure are.
const topSample = 500 * time.Millisecond

// killWait is how long host.kill waits for a process to go after its signal.
const killWait = 10 * time.Second

// hostCall serves the host methods. Reading them is monitoring, as the reports are;
// a process's command line can carry secrets, so it comes only with command
// execution, and ending a process needs it too.
func (a *Agent) hostCall(ctx context.Context, m envelope) (any, error) {
	exec := a.config().Capabilities.Exec
	switch m.Method {
	case "host.top":
		var p struct {
			Sort  string `json:"sort"`
			Limit int    `json:"limit"`
		}
		if len(m.Params) > 0 && json.Unmarshal(m.Params, &p) != nil {
			return nil, errors.New("invalid_params")
		}
		p.Sort = cmp.Or(p.Sort, "cpu")
		if !slices.Contains(topSorts, p.Sort) {
			return nil, errors.New("unsupported_sort")
		}
		if p.Limit <= 0 {
			p.Limit = 10
		}
		return hostTop(ctx, p.Sort, min(p.Limit, 50), exec)
	case "host.details":
		return hostDetails(ctx, a.Collector.Info())
	case "host.kill":
		if !exec {
			return nil, errors.New("capability_disabled")
		}
		var p struct {
			PID   int  `json:"pid"`
			Force bool `json:"force"`
		}
		if json.Unmarshal(m.Params, &p) != nil || protectedPID(p.PID) || p.PID == os.Getpid() {
			return nil, errors.New("invalid_pid")
		}
		asked, err := signalProcess(p.PID, p.Force)
		if err != nil {
			return nil, err
		}
		// A process that can only be ended by force says so at once instead of after the wait.
		return map[string]any{"pid": p.PID, "exited": asked && waitExited(ctx, p.PID, killWait)}, nil
	}
	return nil, errors.New("unsupported_method")
}

func waitExited(ctx context.Context, pid int, limit time.Duration) bool {
	deadline := time.Now().Add(limit)
	for {
		if !processAlive(pid) {
			return true
		}
		if time.Now().After(deadline) {
			return false
		}
		select {
		case <-ctx.Done():
			return !processAlive(pid)
		case <-time.After(200 * time.Millisecond):
		}
	}
}

// rank orders by the asked figure, the busiest first, and keeps limit rows.
func rank(list []HostProcess, sort string, limit int) []HostProcess {
	key := func(p HostProcess) float64 {
		switch sort {
		case "mem":
			return float64(p.RSS)
		case "io":
			return float64(p.ReadRate + p.WriteRate)
		case "conn", "net":
			return float64(p.Conns)
		}
		return p.CPU
	}
	slices.SortStableFunc(list, func(a, b HostProcess) int {
		return cmp.Or(cmp.Compare(key(b), key(a)), cmp.Compare(b.CPU, a.CPU), cmp.Compare(a.PID, b.PID))
	})
	if sort == "conn" || sort == "net" {
		list = slices.DeleteFunc(list, func(p HostProcess) bool { return p.Conns == 0 })
	}
	return list[:min(limit, len(list))]
}

func rankRates(list []HostRate, limit int) []HostRate {
	slices.SortStableFunc(list, func(a, b HostRate) int {
		return cmp.Or(cmp.Compare(b.RXRate+b.TXRate, a.RXRate+a.TXRate), cmp.Compare(a.Name, b.Name))
	})
	return list[:min(limit, len(list))]
}

// interfaces lists the machine's interfaces with their addresses; extra adds
// what only the platform knows (link speed, byte counters).
func interfaces(extra func(name string, index int) (speed int, rx, tx uint64)) []HostIface {
	list, _ := net.Interfaces()
	out := []HostIface{}
	for _, i := range list {
		if i.Flags&net.FlagLoopback != 0 || strings.HasPrefix(i.Name, "veth") {
			continue
		}
		iface := HostIface{Name: i.Name, MAC: i.HardwareAddr.String(), MTU: i.MTU, Up: i.Flags&net.FlagUp != 0}
		addrs, _ := i.Addrs()
		for _, a := range addrs {
			iface.Addrs = append(iface.Addrs, a.String())
		}
		if extra != nil {
			iface.Speed, iface.RX, iface.TX = extra(i.Name, i.Index)
		}
		out = append(out, iface)
	}
	return out
}

func cut(s string, n int) string {
	if r := []rune(s); len(r) > n {
		return string(r[:n]) + "…"
	}
	return s
}
