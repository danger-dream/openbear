//go:build linux || darwin || windows

package node

import (
	"encoding/json"
	"fmt"
	"net"
	"net/netip"
	"net/url"
	"os"
	"time"
)

// meshStale: a status.json older than this is from a data plane that hangs
// or is gone. It writes one every second; applying a config can hold it up
// for a few.
const meshStale = 15 * time.Second

func (a *Agent) platformMeshLink() meshLink { return a.meshNode() }

// carries: address's host is an IP inside the network prefix of the config
// in applied.json, the one the data plane last applied.
func (h *meshHost) carries(address string) bool {
	u, err := url.Parse(address)
	if err != nil {
		return false
	}
	ip, err := netip.ParseAddr(u.Hostname())
	if err != nil {
		return false
	}
	p, ok := h.appliedPrefix()
	return ok && p.Contains(ip.Unmap())
}

// appliedPrefix reads the network prefix of applied.json, again only when
// the file changed.
func (h *meshHost) appliedPrefix() (netip.Prefix, bool) {
	path := h.path("applied.json")
	info, err := os.Stat(path)
	h.routeMu.Lock()
	defer h.routeMu.Unlock()
	if err != nil {
		h.applied = appliedPrefix{}
		return netip.Prefix{}, false
	}
	if c := h.applied; c.read && c.mod.Equal(info.ModTime()) && c.size == info.Size() {
		return c.prefix, c.ok
	}
	c := appliedPrefix{read: true, mod: info.ModTime(), size: info.Size()}
	if data, err := os.ReadFile(path); err == nil {
		c.prefix, c.ok = networkPrefix(data)
	}
	h.applied = c
	return c.prefix, c.ok
}

type appliedPrefix struct {
	read   bool
	mod    time.Time
	size   int64
	prefix netip.Prefix
	ok     bool
}

// meshAddressing is what of a config decides the addresses on the mesh
// interface.
type meshAddressing struct {
	Network struct {
		Prefix string `json:"prefix"`
	} `json:"network"`
	Self struct {
		Address string `json:"address"`
	} `json:"self"`
}

func networkPrefix(config []byte) (netip.Prefix, bool) {
	var c meshAddressing
	if json.Unmarshal(config, &c) != nil {
		return netip.Prefix{}, false
	}
	p, err := netip.ParsePrefix(c.Network.Prefix)
	if err != nil {
		return netip.Prefix{}, false
	}
	return p.Masked(), true
}

// up: status.json is fresh, from a live process, ready, and the interface it
// names, once seen, is still there.
func (h *meshHost) up() (bool, string) {
	path := h.path("status.json")
	info, err := os.Stat(path)
	if err != nil {
		return false, "no data plane status"
	}
	if age := time.Since(info.ModTime()); age > meshStale {
		return false, fmt.Sprintf("data plane status not updated for %s", age.Round(time.Second))
	}
	var s struct {
		Pid   int    `json:"pid"`
		Ready bool   `json:"ready"`
		Iface string `json:"iface"`
	}
	// Windows may refuse a read while the data plane replaces the file.
	readable := false
	for try := 0; try < 3 && !readable; try++ {
		if try > 0 {
			time.Sleep(50 * time.Millisecond)
		}
		data, err := os.ReadFile(path)
		readable = err == nil && json.Unmarshal(data, &s) == nil
	}
	if !readable {
		return false, "unreadable data plane status"
	}
	if _, alive := processStart(s.Pid); s.Pid <= 0 || !alive {
		return false, "data plane not running"
	}
	if !s.Ready {
		return false, "data plane not ready"
	}
	if s.Iface != "" {
		// An interface this process does not find by the data plane's name
		// is never taken for gone.
		_, err := net.InterfaceByName(s.Iface)
		h.routeMu.Lock()
		seen := h.iface == s.Iface
		if err == nil {
			h.iface = s.Iface
		}
		h.routeMu.Unlock()
		if err != nil && seen {
			return false, "interface " + s.Iface + " is gone"
		}
	}
	return true, ""
}

// takesDown names what a mesh call would do to the running data plane that
// ends connections through it, or is "" when it would not: an upgrade that
// replaces the copy a running service runs, removing the mesh, and applying
// a config that changes this node's mesh address or the mesh's prefix.
func (h *meshHost) takesDown(method string, raw json.RawMessage) string {
	if h.record() == nil {
		return ""
	}
	switch method {
	case "mesh.upgrade":
		if build := h.build(); build != "" && h.service.state() == "running" {
			if current, _ := fileHash(h.binary()); current != build {
				return "the mesh upgrade"
			}
		}
	case "mesh.uninstall":
		if h.service.state() == "running" {
			return "removing the mesh"
		}
	case "mesh.apply":
		var p struct {
			Config json.RawMessage `json:"config"`
		}
		applied, err := os.ReadFile(h.path("applied.json"))
		if err != nil || json.Unmarshal(raw, &p) != nil {
			return ""
		}
		var next, cur meshAddressing
		if json.Unmarshal(p.Config, &next) != nil || json.Unmarshal(applied, &cur) != nil {
			return ""
		}
		if next != cur && h.service.state() == "running" {
			return "applying a config that readdresses the mesh"
		}
	}
	return ""
}
