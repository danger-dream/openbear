package proxy

import (
	"encoding/json"
	"net/netip"
	"slices"
	"sync"
)

// Service runs a node's entries and exit as its mesh config says.
type Service struct {
	mu        sync.Mutex
	exit      *exitServer
	exitKey   string
	exitErr   string
	exitStats exitStats
	exitConns conns
	// entries by name, the one a controller from before Entries sends as "".
	entries map[string]*runningEntry
	pool    *pool
	local   localAddrs
	last    func()
}

type runningEntry struct {
	srv   *entryServer
	key   string
	users map[string]*userStats
	open  *conns
}

// Apply starts, changes or stops the entry and exit for c (nil: neither);
// self is this node's mesh address, mesh the prefixes no exit dials. What
// did not change keeps running, its open connections included.
func (s *Service) Apply(c *Config, self netip.Addr, mesh []netip.Prefix) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.last = func() { s.apply(c, self, mesh) }
	s.apply(c, self, mesh)
}

// Retry applies the last config again where a listener failed: an address
// that was not there yet.
func (s *Service) Retry() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.last == nil {
		return
	}
	retry := false
	if s.exitErr != "" {
		s.exitKey, retry = "", true
	}
	for _, e := range s.entries {
		if len(e.srv.errs) > 0 {
			e.key, retry = "", true
		}
	}
	if retry {
		s.last()
	}
}

func key(v ...any) string {
	b, _ := json.Marshal(v)
	return string(b)
}

func (s *Service) apply(c *Config, self netip.Addr, mesh []netip.Prefix) {
	var exit *Exit
	var list []Entry
	port := 0
	if c != nil {
		exit, port = c.Exit, c.Port
		if c.Entry != nil {
			e := *c.Entry
			e.Name = ""
			list = append(list, e)
		}
		list = append(list, c.Entries...)
	}
	if k := key(exit, self, port, mesh); k != s.exitKey {
		if s.exit != nil {
			s.exit.close()
			s.exit = nil
		}
		s.exitKey, s.exitErr = k, ""
		if exit != nil {
			x, err := startExit(netip.AddrPortFrom(self, uint16(port)), exit, mesh, &s.exitStats, &s.local, &s.exitConns)
			if err != nil {
				s.exitErr = err.Error()
			}
			s.exit = x
		}
		// Tunnels open run on, but for an entry or a target the exit no longer allows.
		var p *policy
		if exit != nil {
			p = &policy{lan: exit.LAN, mesh: mesh, self: &s.local}
		}
		s.exitConns.cut(func(from, to netip.Addr) bool {
			return p == nil || !slices.Contains(exit.Clients, from) || to.IsValid() && !p.allowed(to)
		})
	}
	keys := map[string]string{}
	for _, e := range list {
		keys[e.Name] = key(e, port)
	}
	newPort := s.pool != nil && s.pool.port != port
	next := map[string]*Entry{}
	for i := range list {
		next[list[i].Name] = &list[i]
	}
	// Clients of a port that changed go on with it, but not past a new
	// password or a closed address.
	open := map[string]*conns{}
	for name, r := range s.entries {
		if newPort || keys[name] != r.key {
			r.srv.close()
			delete(s.entries, name)
			if n := next[name]; n == nil || n.Password != r.srv.cfg.Password ||
				slices.ContainsFunc(r.srv.cfg.Listen, func(l netip.AddrPort) bool { return !slices.Contains(n.Listen, l) }) {
				r.open.cut(nil)
			} else {
				open[name] = r.open
			}
		}
	}
	if s.pool != nil && (len(list) == 0 || newPort) {
		s.pool.close()
		s.pool = nil
	}
	if len(list) == 0 {
		return
	}
	if s.pool == nil {
		s.pool = newPool(port)
	}
	if s.entries == nil {
		s.entries = map[string]*runningEntry{}
	}
	var warm []netip.Addr
	for _, e := range list {
		warm = append(warm, e.warmed()...)
		if s.entries[e.Name] != nil {
			continue
		}
		users := map[string]*userStats{}
		for _, u := range e.Users {
			users[u.Name] = &userStats{}
		}
		o := open[e.Name]
		if o == nil {
			o = &conns{}
		}
		s.entries[e.Name] = &runningEntry{srv: startEntry(&e, s.pool, users, o), key: keys[e.Name], users: users, open: o}
	}
	s.pool.warm(warm)
}

// Close stops the entry and the exit.
func (s *Service) Close() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.apply(nil, netip.Addr{}, nil)
	s.last = nil
}

type Status struct {
	Exit    *ExitStatus   `json:"exit,omitempty"`
	Entry   *EntryStatus  `json:"entry,omitempty"`
	Entries []EntryStatus `json:"entries,omitempty"`
}

type ExitStatus struct {
	Listen   string `json:"listen"`
	Error    string `json:"error,omitempty"`
	Active   int64  `json:"active"`
	Opened   int64  `json:"opened"`
	Refused  int64  `json:"refused"`
	Failed   int64  `json:"failed"`
	BytesIn  int64  `json:"bytesIn"`
	BytesOut int64  `json:"bytesOut"`
}

type EntryStatus struct {
	Name     string       `json:"name,omitempty"`
	Listen   []string     `json:"listen"`
	Errors   []string     `json:"errors,omitempty"`
	Rejected int64        `json:"rejected"`
	Users    []UserStatus `json:"users"`
}

type UserStatus struct {
	Name      string      `json:"name"`
	Exit      string      `json:"exit,omitempty"` // the exit its last tunnel went to
	Opened    int64       `json:"opened"`
	Failed    int64       `json:"failed"`
	Active    int64       `json:"active"`
	BytesUp   int64       `json:"bytesUp"`
	BytesDown int64       `json:"bytesDown"`
	Exits     []ExitState `json:"exits"`
	// Picked: a latency user's exit now, the one with the lowest round trip.
	Picked string `json:"picked,omitempty"`
}

// ExitState is how an entry sees one exit of a user.
type ExitState struct {
	Address string `json:"address"`
	Ready   int    `json:"ready"` // tunnels open ahead of clients
	Down    bool   `json:"down,omitempty"`
	Error   string `json:"error,omitempty"`
	// RTTMs: how long its tunnels take to open, smoothed; measured once one has.
	RTTMs *float64 `json:"rttMs,omitempty"`
}

// Status is nil while the node is neither entry nor exit.
func (s *Service) Status() *Status {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.exit == nil && s.exitErr == "" && len(s.entries) == 0 {
		return nil
	}
	out := &Status{}
	if s.exit != nil || s.exitErr != "" {
		x := &ExitStatus{Error: s.exitErr, Active: s.exitStats.Active.Load(), Opened: s.exitStats.Opened.Load(), Refused: s.exitStats.Refused.Load(),
			Failed: s.exitStats.Failed.Load(), BytesIn: s.exitStats.BytesIn.Load(), BytesOut: s.exitStats.BytesOut.Load()}
		if s.exit != nil {
			x.Listen = s.exit.ln.Addr().String()
		}
		out.Exit = x
	}
	names := make([]string, 0, len(s.entries))
	for name := range s.entries {
		names = append(names, name)
	}
	slices.Sort(names)
	for _, name := range names {
		es := s.entryStatus(s.entries[name])
		if name == "" {
			out.Entry = es
		} else {
			out.Entries = append(out.Entries, *es)
		}
	}
	return out
}

func (s *Service) entryStatus(r *runningEntry) *EntryStatus {
	e := r.srv
	es := &EntryStatus{Name: e.cfg.Name, Errors: slices.Clone(e.errs), Rejected: e.rejected.Load(), Users: []UserStatus{}}
	for _, ln := range e.listeners {
		es.Listen = append(es.Listen, ln.Addr().String())
	}
	for _, u := range e.cfg.Users {
		st := r.users[u.Name]
		us := UserStatus{Name: u.Name, Opened: st.Opened.Load(), Failed: st.Failed.Load(), Active: st.Active.Load(),
			BytesUp: st.BytesUp.Load(), BytesDown: st.BytesDown.Load()}
		if a := st.exit.Load(); a != nil {
			us.Exit = a.String()
		}
		for _, a := range u.Exits {
			p := s.pool.status(a)
			x := ExitState{Address: a.String(), Ready: p.Ready, Down: p.Down, Error: p.Error}
			if p.RTT > 0 {
				ms := float64(p.RTT.Microseconds()) / 1000
				x.RTTMs = &ms
			}
			us.Exits = append(us.Exits, x)
		}
		if a, ok := e.picked(u.Name); ok {
			us.Picked = a.String()
		}
		es.Users = append(es.Users, us)
	}
	return es
}
