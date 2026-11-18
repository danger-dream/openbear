// Package fast is the mesh's fast relay: a connection to a fast port, a port
// on a node's mesh address the controller declared fast, is carried hop by hop
// over plain TCP between nodes to that port, as fast as the lines go. The path
// is the mesh's own: its fixed paths, then its routes and their runners-up,
// probed and kept warm while the port is in use. Only a sealed header naming
// the destination goes ahead of the data; the data itself is spliced through
// unencrypted. A hop it cannot dial directly is reached through the mesh,
// which encrypts that leg alone.
package fast

import (
	"encoding/base64"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"strconv"
)

// Config is a node's part of the fast relay, from the mesh config.
type Config struct {
	// Port is this node's fast relay listen port; 0 when nothing dials it
	// (a member that is no fast port's destination).
	Port int `json:"port,omitempty"`
	// MeshPort is the default fast relay port of the other nodes, dialed over
	// their mesh addresses when MeshPorts does not name another.
	MeshPort int `json:"meshPort"`
	// MeshPorts overrides the fast relay listen port of nodes whose
	// listen.fast differs from MeshPort, by node id.
	MeshPorts map[string]int `json:"meshPorts,omitempty"`
	// Keys seal headers, base64 of 32 bytes, the newest first. A second one,
	// while there is one, still opens what nodes not given the newest seal.
	Keys []string `json:"keys"`
	// Nodes are the mesh addresses, by node id, of the nodes that run fast
	// relay (this one's included): the only ones a path may cross.
	Nodes map[string]netip.Addr `json:"nodes"`
	// Next are the nodes this node dials directly, by id: host:port of their
	// relay port.
	Next map[string]string `json:"next,omitempty"`
	// Dial are the links, [from, to], over which from dials to's relay port
	// directly.
	Dial [][2]string `json:"dial,omitempty"`
	// Targets are the fast ports this node's connections may take.
	Targets []Target `json:"targets,omitempty"`
	// Paths is how many paths to a destination are kept warm.
	Paths int `json:"paths,omitempty"`
	// Capture takes this machine's and its LAN's connections to Targets
	// (nftables on Linux, the data plane elsewhere).
	Capture bool `json:"capture,omitempty"`
}

// Target is a fast port: Port on node Node's mesh address. Src, when set,
// are the client addresses allowed to use it (as access rules see them).
type Target struct {
	Node  string         `json:"node"`
	Port  uint16         `json:"port"`
	Proto string         `json:"proto"`
	Src   []netip.Prefix `json:"src,omitempty"`
}

const (
	maxTargets   = 256
	defaultPaths = 3
	maxPaths     = 8
)

// Rule matches mesh.Rule: Src or Dst empty matches any address.
type Rule struct {
	Src   []netip.Prefix `json:"src"`
	Dst   []netip.Prefix `json:"dst"`
	Proto string         `json:"proto"`
	Ports [][2]uint16    `json:"ports,omitempty"`
}

// Check reports what makes c unusable.
func (c *Config) Check() error {
	if c.Port < 0 || c.Port > 65535 {
		return errors.New("fast.port is not a port")
	}
	if c.MeshPort < 1 || c.MeshPort > 65535 {
		return errors.New("fast.meshPort is not a port")
	}
	for id, p := range c.MeshPorts {
		if id == "" || p < 1 || p > 65535 {
			return fmt.Errorf("fast.meshPorts[%q] is not a port", id)
		}
	}
	if len(c.Keys) == 0 || len(c.Keys) > 2 {
		return errors.New("fast.keys must hold one or two keys")
	}
	for i, k := range c.Keys {
		if b, err := base64.StdEncoding.DecodeString(k); err != nil || len(b) != 32 {
			return fmt.Errorf("fast.keys[%d] is not base64 of 32 bytes", i)
		}
	}
	for id, a := range c.Nodes {
		if id == "" || !a.Is4() {
			return fmt.Errorf("fast.nodes[%q] is not an IPv4 address", id)
		}
	}
	for id, hp := range c.Next {
		if _, ok := c.Nodes[id]; !ok {
			return fmt.Errorf("fast.next[%q] is not in fast.nodes", id)
		}
		host, port, err := net.SplitHostPort(hp)
		if p, perr := strconv.Atoi(port); err != nil || host == "" || perr != nil || p < 1 || p > 65535 {
			return fmt.Errorf("fast.next[%q] %q is not host:port", id, hp)
		}
	}
	if len(c.Dial) > 16384 {
		return errors.New("fast.dial has more than 16384 links")
	}
	if len(c.Targets) > maxTargets {
		return fmt.Errorf("fast.targets has more than %d ports", maxTargets)
	}
	seen := map[[3]string]bool{}
	for i, t := range c.Targets {
		if _, ok := c.Nodes[t.Node]; !ok {
			return fmt.Errorf("fast.targets[%d].node %q is not in fast.nodes", i, t.Node)
		}
		if t.Port == 0 {
			return fmt.Errorf("fast.targets[%d].port is not a port", i)
		}
		switch t.Proto {
		case "tcp", "udp", "any":
		default:
			return fmt.Errorf("fast.targets[%d].proto %q is unknown", i, t.Proto)
		}
		k := [3]string{t.Node, strconv.Itoa(int(t.Port)), t.Proto}
		if seen[k] {
			return fmt.Errorf("fast.targets[%d] is repeated", i)
		}
		seen[k] = true
		for _, p := range t.Src {
			if !p.IsValid() || !p.Addr().Is4() || p.Masked() != p {
				return fmt.Errorf("fast.targets[%d].src %v is not a masked IPv4 subnet", i, p)
			}
		}
	}
	if c.Paths < 0 || c.Paths > maxPaths {
		return fmt.Errorf("fast.paths is not 0 to %d", maxPaths)
	}
	return nil
}

func (c *Config) peerMeshPort(id string) int {
	if p := c.MeshPorts[id]; p > 0 {
		return p
	}
	return c.MeshPort
}

func (c *Config) pathCount() int {
	if c.Paths > 0 {
		return c.Paths
	}
	return defaultPaths
}

func ruleAllows(rules []Rule, proto byte, src, dst netip.Addr, port uint16) bool {
	if port == 0 {
		return false
	}
	src, dst = src.Unmap(), dst.Unmap()
	for _, r := range rules {
		if !prefixMatches(r.Src, src) || !prefixMatches(r.Dst, dst) {
			continue
		}
		switch r.Proto {
		case "", "any":
		case "tcp":
			if proto != protoTCP {
				continue
			}
		case "udp":
			if proto != protoUDP {
				continue
			}
		default:
			continue
		}
		if len(r.Ports) > 0 && !portMatches(r.Ports, port) {
			continue
		}
		return true
	}
	return false
}

func prefixMatches(list []netip.Prefix, a netip.Addr) bool {
	if len(list) == 0 {
		return true
	}
	for _, p := range list {
		if p.Contains(a) {
			return true
		}
	}
	return false
}

func portMatches(ranges [][2]uint16, port uint16) bool {
	for _, r := range ranges {
		if port >= r[0] && port <= r[1] {
			return true
		}
	}
	return false
}
