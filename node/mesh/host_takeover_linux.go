package mesh

import (
	"encoding/json"
	"fmt"
	"net/netip"
	"os"
	"path/filepath"
	"slices"
	"strings"
)

// Flags "ip route show" prints that "ip route add" does not take.
var routeShowFlags = []string{"linkdown", "dead", "offload", "trap", "rt_offload", "rt_trap", "rt_offload_failed"}

// held: this machine's routes outside the mesh, and those Takeover replaced.
func (h *linuxHost) held() []heldRoute {
	h.mu.Lock()
	defer h.mu.Unlock()
	data, _ := os.ReadFile(h.routeFile)
	out := parseRouteTable(data, h.name)
	for p, lines := range h.taken {
		out = append(out, heldRoute{p, takenDev(lines)})
	}
	return out
}

func (h *linuxHost) restored() []string {
	h.mu.Lock()
	defer h.mu.Unlock()
	notes := h.notes
	h.notes = nil
	return notes
}

// take keeps the main table's routes for exactly r outside the mesh, which
// r is about to replace (or outrank), on disk before r goes in.
func (h *linuxHost) take(r netip.Prefix) error {
	if _, ok := h.taken[r]; ok {
		return nil
	}
	out, err := h.x("ip", "-4", "route", "show", "exact", r.String(), "table", "main")
	if err != nil {
		return err
	}
	var lines []string
	for _, line := range strings.Split(out, "\n") {
		f := strings.Fields(line)
		if i := slices.Index(f, "dev"); len(f) == 0 || i >= 0 && i+1 < len(f) && f[i+1] == h.name {
			continue
		}
		lines = append(lines, strings.Join(f, " "))
	}
	if len(lines) == 0 {
		return nil
	}
	h.taken[r] = lines
	if err := h.saveTaken(); err != nil {
		delete(h.taken, r)
		return err
	}
	h.notes = append(h.notes, fmt.Sprintf("gateway subnet %v takes over this machine's route: %s", r, strings.Join(lines, "; ")))
	return nil
}

// putBack restores what r replaced, once r is out of the main table. A
// route that cannot come back (its interface is gone) is said and dropped.
func (h *linuxHost) putBack(r netip.Prefix) {
	for _, line := range h.taken[r] {
		args := []string{"ip", "-4", "route", "add"}
		for _, f := range strings.Fields(line) {
			if !slices.Contains(routeShowFlags, f) {
				args = append(args, f)
			}
		}
		if _, err := h.x(args...); err != nil && !strings.Contains(err.Error(), "File exists") {
			h.notes = append(h.notes, fmt.Sprintf("could not put back this machine's route %s: %v", line, err))
		} else {
			h.notes = append(h.notes, "put back this machine's route: "+line)
		}
	}
	delete(h.taken, r)
	if err := h.saveTaken(); err != nil {
		h.notes = append(h.notes, "could not save "+TakenFile+": "+err.Error())
	}
}

// releaseTaken puts back what each taken route replaced unless keep still
// installs it over them.
func (h *linuxHost) releaseTaken(keep []netip.Prefix) {
	for r := range h.taken {
		if !slices.Contains(keep, r) {
			h.putBack(r)
		}
	}
}

func (h *linuxHost) saveTaken() error {
	if h.takenDir == "" {
		return nil
	}
	path := filepath.Join(h.takenDir, TakenFile)
	if len(h.taken) == 0 {
		if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
			return err
		}
		return nil
	}
	saved := map[string][]string{}
	for p, lines := range h.taken {
		saved[p.String()] = lines
	}
	raw, _ := json.Marshal(saved)
	if err := os.WriteFile(path+".tmp", raw, 0o600); err != nil {
		return err
	}
	return os.Rename(path+".tmp", path)
}
