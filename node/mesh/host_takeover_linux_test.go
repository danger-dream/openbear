package mesh

import (
	"errors"
	"net/netip"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

// routeTableHost is a main table "ip route" edits as the kernel does for
// these commands: replace takes the place of a route of the same prefix
// and metric, add refuses one.
type routeTableHost struct {
	lines []string
}

func metricOf(f []string) string {
	if i := slices.Index(f, "metric"); i >= 0 && i+1 < len(f) {
		return f[i+1]
	}
	return "0"
}

func (r *routeTableHost) exec(args ...string) (string, error) {
	if len(args) < 4 || args[0] != "ip" || args[2] != "route" {
		return "", nil
	}
	same := func(line string, f []string) bool {
		g := strings.Fields(line)
		return g[0] == f[0] && metricOf(g) == metricOf(f)
	}
	f := args[4:]
	switch args[3] {
	case "show":
		var out []string
		for _, line := range r.lines {
			if strings.Fields(line)[0] == f[1] {
				out = append(out, line)
			}
		}
		return strings.Join(out, "\n"), nil
	case "replace":
		r.lines = slices.DeleteFunc(r.lines, func(line string) bool { return same(line, f) })
		r.lines = append(r.lines, strings.Join(f, " "))
	case "add":
		if slices.ContainsFunc(r.lines, func(line string) bool { return same(line, f) }) {
			return "", errors.New(strings.Join(args, " ") + ": exit status 2: RTNETLINK answers: File exists")
		}
		r.lines = append(r.lines, strings.Join(f, " "))
	case "del":
		r.lines = slices.DeleteFunc(r.lines, func(line string) bool { return strings.HasPrefix(line, f[0]+" dev "+f[2]) })
	}
	return "", nil
}

func TestTakeoverReplacesAndPutsBack(t *testing.T) {
	p := netip.MustParsePrefix
	lan := p("192.168.10.0/24")
	table := &routeTableHost{lines: []string{"192.168.10.0/24 dev wg0 proto static scope link linkdown", "10.20.0.0/16 dev wg0 scope link"}}
	dir := t.TempDir()
	host := func() *linuxHost {
		h := newLinuxHost("obmesh0", table.exec, func(string) bool { return false })
		h.forwardFile, h.routeFile = filepath.Join(dir, "ip_forward"), filepath.Join(dir, "none")
		h.takenDir, h.taken = dir, loadTaken(dir)
		return h
	}
	cfg := hostConfig{Address: netip.MustParseAddr("100.96.0.3"), Prefix: p("100.96.0.0/16"), MTU: 1400}
	taking := cfg
	taking.Routes, taking.Takeover = []netip.Prefix{cfg.Prefix, lan}, []netip.Prefix{lan}
	plain := cfg
	plain.Routes = []netip.Prefix{cfg.Prefix}

	h := host()
	if err := h.apply(taking); err != nil {
		t.Fatal(err)
	}
	if want := []string{"10.20.0.0/16 dev wg0 scope link", "100.96.0.0/16 dev obmesh0 src 100.96.0.3", "192.168.10.0/24 dev obmesh0 src 100.96.0.3"}; !slices.Equal(table.lines, want) {
		t.Fatalf("taken over: %q", table.lines)
	}
	if got := loadTaken(dir)[lan]; !slices.Equal(got, []string{"192.168.10.0/24 dev wg0 proto static scope link linkdown"}) {
		t.Fatalf("kept on disk: %q", got)
	}
	if held := h.held(); !slices.Contains(held, heldRoute{lan, "wg0"}) {
		t.Fatalf("what it replaced is not held: %v", held)
	}
	if notes := h.restored(); len(notes) != 1 || !strings.Contains(notes[0], "takes over") {
		t.Fatalf("notes %q", notes)
	}
	// Applied again, nothing is kept twice.
	if err := h.apply(taking); err != nil || len(h.taken) != 1 || len(h.restored()) != 0 {
		t.Fatalf("again: %v %v", err, h.taken)
	}

	// Released: the mesh's route goes, the machine's comes back without the flag add refuses.
	if err := h.apply(plain); err != nil {
		t.Fatal(err)
	}
	if want := []string{"10.20.0.0/16 dev wg0 scope link", "100.96.0.0/16 dev obmesh0 src 100.96.0.3", "192.168.10.0/24 dev wg0 proto static scope link"}; !slices.Equal(table.lines, want) {
		t.Fatalf("released: %q", table.lines)
	}
	if _, err := os.Stat(filepath.Join(dir, TakenFile)); !os.IsNotExist(err) {
		t.Fatalf("%s left behind: %v", TakenFile, err)
	}
	if notes := h.restored(); len(notes) != 1 || !strings.Contains(notes[0], "put back") {
		t.Fatalf("notes %q", notes)
	}

	// Killed while taken over: the next data plane puts it back when it is no longer wanted.
	if err := h.apply(taking); err != nil {
		t.Fatal(err)
	}
	table.lines = slices.DeleteFunc(table.lines, func(line string) bool { return strings.Contains(line, "obmesh0") })
	h = host()
	if err := h.apply(plain); err != nil {
		t.Fatal(err)
	}
	if !slices.Contains(table.lines, "192.168.10.0/24 dev wg0 proto static scope link") || len(loadTaken(dir)) != 0 {
		t.Fatalf("after a restart: %q", table.lines)
	}

	// Stopped while taken over: the same.
	if err := h.apply(taking); err != nil {
		t.Fatal(err)
	}
	h.close()
	if !slices.Contains(table.lines, "192.168.10.0/24 dev wg0 proto static scope link") || slices.Contains(table.lines, "192.168.10.0/24 dev obmesh0 src 100.96.0.3") {
		t.Fatalf("after close: %q", table.lines)
	}

	// A route that cannot come back is said and dropped.
	table.lines = []string{"192.168.10.0/24 dev wg0 scope link"}
	h = host()
	if err := h.apply(taking); err != nil {
		t.Fatal(err)
	}
	h.x = func(args ...string) (string, error) {
		if slices.Contains(args, "add") {
			return "", errors.New("Cannot find device \"wg0\"")
		}
		return table.exec(args...)
	}
	h.restored()
	if err := h.apply(plain); err != nil {
		t.Fatal(err)
	}
	if notes := h.restored(); len(notes) != 1 || !strings.Contains(notes[0], "could not put back") || len(h.taken) != 0 {
		t.Fatalf("notes %q, taken %v", notes, h.taken)
	}
}
