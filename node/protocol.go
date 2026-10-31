package node

import "math"

// Version is overwritten at build time with -X openbear/node.Version=...
var Version = "dev"

// ProtocolVersion is the newest controller <-> node protocol this build
// speaks and ProtocolMin the oldest. A node and a controller work together
// when their ranges overlap, at the highest number both have (the
// controller's range is app/servers/compat.py PROTOCOL). A new protocol
// raises ProtocolVersion; the old one may be dropped by raising ProtocolMin
// one release later at the earliest, so a node one release behind still
// works. The release script reads ProtocolMin from this file.
const ProtocolVersion = 1
const ProtocolMin = 1

// Features names the methods added after protocol 1, so the controller can
// tell an older node apart before calling one.
var Features = []string{"fs.edit", "jobs", "exec.shell", "mesh", "net", "access.mesh", "net.ports", "ddns", "http.local", "mesh.confirmed", "fs.transfer"}

type Info struct {
	Hostname string `json:"hostname"`
	// Platform is GOOS: linux, darwin or windows; OS is the release name.
	Platform       string `json:"platform"`
	OS             string `json:"os"`
	Kernel         string `json:"kernel"`
	Arch           string `json:"arch"`
	CPU            string `json:"cpu"`
	Cores          int    `json:"cores"`
	Virtualization string `json:"virtualization"`
	MemTotal       int64  `json:"memTotal"`
	SwapTotal      int64  `json:"swapTotal"`
	DiskTotal      int64  `json:"diskTotal"`
	IPv4           string `json:"ipv4"`
	IPv6           string `json:"ipv6"`
	// IPv4Proxy: the exit echo services abroad see when it differs from IPv4 (a proxy).
	IPv4Proxy string `json:"ipv4Proxy,omitempty"`
	// MachineID lets a reusable registration code recognise a reinstall.
	MachineID string `json:"machineId"`
}

type Usage struct {
	Total int64 `json:"total"`
	Used  int64 `json:"used"`
}

// Traffic counters are cumulative since boot; the controller derives billing
// periods from their differences, so reconnects lose nothing.
type Traffic struct {
	RX     uint64 `json:"rx"`
	TX     uint64 `json:"tx"`
	RXRate uint64 `json:"rxRate"`
	TXRate uint64 `json:"txRate"`
}

type DiskIO struct {
	ReadRate  uint64 `json:"readRate"`
	WriteRate uint64 `json:"writeRate"`
}

type Sample struct {
	CPU float64 `json:"cpu"`
	// Steal is time the hypervisor gave to other guests: the direct sign of an
	// oversold VPS host.
	Steal     float64    `json:"steal"`
	IOWait    float64    `json:"iowait"`
	Mem       Usage      `json:"mem"`
	Swap      Usage      `json:"swap"`
	Load      [3]float64 `json:"load"`
	Disk      Usage      `json:"disk"`
	IO        DiskIO     `json:"io"`
	Net       Traffic    `json:"net"`
	TCP       int        `json:"tcp"`
	UDP       int        `json:"udp"`
	Processes int        `json:"processes"`
	Uptime    int64      `json:"uptime"`
	Boot      int64      `json:"boot"`
	NodeRSS   int64      `json:"nodeRss"`
}

type Capabilities struct {
	Exec     bool `json:"exec"`
	Terminal bool `json:"terminal"`
	// Browser lets the controller drive a browser on this machine, with its
	// network and logins. Off unless the machine's owner turns it on.
	Browser bool `json:"browser"`
}

// vmVendors maps hardware vendor or product text to systemd-detect-virt names.
var vmVendors = [][2]string{{"kvm", "kvm"}, {"qemu", "qemu"}, {"vmware", "vmware"}, {"virtualbox", "oracle"}, {"xen", "xen"},
	{"hyper-v", "microsoft"}, {"microsoft corporation", "microsoft"}, {"amazon ec2", "amazon"}, {"google compute", "google"}}

func perSecond(now, prev uint64, elapsed float64) uint64 {
	if now < prev || elapsed <= 0 {
		return 0
	}
	return uint64(float64(now-prev) / elapsed)
}

func round(v float64, digits int) float64 {
	p := math.Pow10(digits)
	return math.Round(v*p) / p
}
