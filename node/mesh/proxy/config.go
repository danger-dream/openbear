// Package proxy is the mesh's exit proxy. An entry takes standard SOCKS5
// and HTTP proxy connections from programs on its machine, its LAN or, for
// an exit's own entry on its mesh address, the mesh; the username names the
// exit. Between nodes a connection rides a TCP tunnel to
// the exit's mesh address, which the mesh already encrypts and
// authenticates: no handshake of its own, the target and the first data go
// out together over a connection opened beforehand, and the entry answers
// its client at once. An exit dials the target from its machine.
package proxy

import (
	"errors"
	"fmt"
	"net/netip"
	"regexp"
	"slices"
)

// Config is a node's part of the exit proxy, from the mesh config.
type Config struct {
	// Port is the tunnel port on every exit's mesh address.
	Port int `json:"port"`
	// Exit is this node as an exit; nil when it is none.
	Exit *Exit `json:"exit,omitempty"`
	// Entry is this node as an entry; nil when it is none. Controllers from
	// before Entries send this one.
	Entry *Entry `json:"entry,omitempty"`
	// Entries are this node's proxy ports, each with its own name, password
	// and users: an exit's own SOCKS5 on its mesh address is one of them.
	Entries []Entry `json:"entries,omitempty"`
}

// Exit says what an exit dials and for whom.
type Exit struct {
	// LAN lets clients reach private and local addresses, this machine's
	// own included; the mesh's never.
	LAN bool `json:"lan,omitempty"`
	// Interface (Linux) and Source bind what the exit dials, past a proxy
	// or a TUN this machine routes its own traffic through.
	Interface string     `json:"interface,omitempty"`
	Source    netip.Addr `json:"source,omitzero"`
	// Clients are the mesh addresses of the entries that may use it.
	Clients []netip.Addr `json:"clients"`
}

// Entry is where an entry listens and where its usernames lead.
type Entry struct {
	// Name tells an entry of Entries apart, in its status too.
	Name   string           `json:"name,omitempty"`
	Listen []netip.AddrPort `json:"listen"`
	// Password is what every username authenticates with. Without one, an
	// entry of Entries takes every client as its Default user.
	Password string `json:"password"`
	// Users are the usernames, each an exit and its backups in order.
	Users []User `json:"users"`
	// Default is the user a client that does not authenticate stands for;
	// "" refuses such clients.
	Default string `json:"default,omitempty"`
	// Strict answers a client only once the exit has reached the target,
	// with the exit's error; otherwise at once.
	Strict bool `json:"strict,omitempty"`
}

// User leads to Exits, the mesh addresses of its exit group in order. Select
// latency takes the one with the lowest round trip first (entryServer.pick);
// anything else, the first that answers.
type User struct {
	Name   string       `json:"name"`
	Exits  []netip.Addr `json:"exits"`
	Select string       `json:"select,omitempty"`
}

const selectLatency = "latency"

var userName = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,31}$`)

// Check rejects a config the proxy cannot run.
func (c *Config) Check() error {
	if c.Exit == nil && c.Entry == nil && len(c.Entries) == 0 {
		return nil
	}
	if c.Port < 1 || c.Port > 65535 {
		return fmt.Errorf("proxy.port %d is out of range", c.Port)
	}
	if x := c.Exit; x != nil {
		if x.Source.IsValid() && x.Source.Zone() != "" {
			return errors.New("proxy.exit.source has a zone")
		}
		if len(x.Interface) > 15 {
			return errors.New("proxy.exit.interface is too long")
		}
	}
	if c.Entry != nil {
		if c.Entry.Password == "" {
			return errors.New("proxy.entry.password needs 1 to 255 bytes")
		}
		if err := c.Entry.check("proxy.entry"); err != nil {
			return err
		}
	}
	if len(c.Entries) > 64 {
		return errors.New("proxy.entries: at most 64")
	}
	names := map[string]bool{}
	for i := range c.Entries {
		e := &c.Entries[i]
		if !userName.MatchString(e.Name) || names[e.Name] {
			return fmt.Errorf("proxy.entries: invalid or repeated name %q", e.Name)
		}
		names[e.Name] = true
		if e.Password == "" && (e.Default == "" || len(e.Users) != 1) {
			return fmt.Errorf("proxy.entries %s: without a password, one user and it the default", e.Name)
		}
		if err := e.check("proxy.entries " + e.Name); err != nil {
			return err
		}
	}
	return nil
}

func (e *Entry) check(p string) error {
	if len(e.Listen) == 0 || len(e.Listen) > 8 {
		return fmt.Errorf("%s.listen needs 1 to 8 addresses", p)
	}
	for _, l := range e.Listen {
		if !l.IsValid() || l.Port() == 0 {
			return fmt.Errorf("%s.listen %v is not an address and port", p, l)
		}
	}
	if len(e.Password) > 255 {
		return fmt.Errorf("%s.password needs 1 to 255 bytes", p)
	}
	names := map[string]bool{}
	for _, u := range e.Users {
		if !userName.MatchString(u.Name) || names[u.Name] {
			return fmt.Errorf("%s.users: invalid or repeated name %q", p, u.Name)
		}
		names[u.Name] = true
		if len(u.Exits) == 0 || len(u.Exits) > 32 || slices.ContainsFunc(u.Exits, func(a netip.Addr) bool { return !a.Is4() }) {
			return fmt.Errorf("%s.users %s: 1 to 32 IPv4 exits", p, u.Name)
		}
	}
	if e.Default != "" && !names[e.Default] {
		return fmt.Errorf("%s.default %q is not a user", p, e.Default)
	}
	// A client that does not authenticate is one of this machine's programs.
	if e.Default != "" && slices.ContainsFunc(e.Listen, func(l netip.AddrPort) bool { return !l.Addr().IsLoopback() }) {
		return fmt.Errorf("%s.default: only on loopback", p)
	}
	return nil
}

func (e *Entry) user(name string) *User {
	for i := range e.Users {
		if e.Users[i].Name == name {
			return &e.Users[i]
		}
	}
	return nil
}
