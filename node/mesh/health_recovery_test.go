package mesh

import (
	"fmt"
	"io"
	"net"
	"sync"
	"testing"
	"time"
)

func freeTCPPort(t *testing.T) int {
	l, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	return l.Addr().(*net.TCPAddr).Port
}

// recoveryLab has relays A and B, A dialing the UDP link. Member M enters
// over TCP and N over UDP, both preferring the same relay, with autoReturn.
// Probing, retry and lease times are shortened so the tests run in seconds.
type recoveryLab struct {
	*lab
	tcp      map[string]int
	revision map[string]int64
	upload   map[string]*float64
	via      map[string]string // "member/relay" -> entry endpoint
	nodes    map[string]*running
	down     map[string]bool
}

func newRecoveryLab(t *testing.T, prefer, other string) *recoveryLab {
	order := []string{prefer, other}
	r := &recoveryLab{lab: newHealthLab(t, spec{relays: []string{"A", "B"}, members: map[string][]string{"M": order, "N": order},
		rules: map[string][]Rule{"A": allowICMP, "B": allowICMP, "M": allowICMP, "N": allowICMP}}),
		tcp: map[string]int{"A": freeTCPPort(t), "B": freeTCPPort(t)}, revision: map[string]int64{}, upload: map[string]*float64{}, via: map[string]string{}, nodes: map[string]*running{}, down: map[string]bool{}}
	health := r.edit
	r.edit = func(name string, c *Config) {
		health(name, c)
		if v := r.revision[name]; v != 0 {
			c.Revision = v
		}
		c.Self.UploadLimitMbps = r.upload[name]
		c.Self.Listen.TCP = r.tcp[name]
		c.Health = &Health{StandbyIntervalMs: 500, ColdRetryMs: 5000, ReturnHealthyMs: 5000, MinResidenceMs: 5000, LeaseMs: 6000, LeaseRefreshMs: 2000}
		if c.Entry != nil {
			c.Entry.AutoReturn = true
			for i := range c.Entry.Candidates {
				p := &c.Entry.Candidates[i]
				if name == "M" {
					p.Endpoint = fmt.Sprintf("tcp://127.0.0.1:%d", r.tcp[p.Relay])
				}
				if v := r.via[name+"/"+p.Relay]; v != "" {
					p.Endpoint = v
				}
			}
		}
	}
	return r
}

func (r *recoveryLab) up() {
	for _, name := range []string{"A", "B", "M", "N"} {
		r.nodes[name] = r.startHealthNode(name)
	}
}

// dump is deferred by the tests: cleanups run after the nodes are closed.
func (r *recoveryLab) dump() {
	if r.t.Failed() {
		for id, n := range r.nodes {
			r.t.Logf("failure %s health=%+v status=%+v paths=%+v events=%+v", id, n.health.Snapshot(), n.status(), n.bind.(*streamBind).Paths(), n.events.snapshot())
		}
	}
}

func (r *recoveryLab) restart(name string) {
	r.nodes[name].close()
	r.nodes[name] = r.startHealthNode(name)
}

// settled is when each member in want is committed to its relay there,
// the running relays agree on that, have committed their link and wait for
// no acknowledgement.
func (r *recoveryLab) settled(want map[string]string) bool {
	for m, relay := range want {
		s := r.nodes[m].status()
		if s.Entry == nil || s.Entry.Relay != relay || s.Entry.Transport == "" || r.nodes[m].health.Snapshot().Error != "" {
			return false
		}
	}
	for _, id := range []string{"A", "B"} {
		if r.down[id] {
			continue
		}
		s := r.nodes[id].status()
		if s.Error != "" {
			return false
		}
		for m, relay := range want {
			if s.Owners[m].Relay != relay {
				return false
			}
		}
		for _, p := range s.Peers {
			if p.Kind == kindRelay && !r.down[p.ID] && p.Transport == "" {
				return false
			}
		}
	}
	return true
}

func (r *recoveryLab) waitSettled(t *testing.T, want map[string]string, within time.Duration) time.Duration {
	t.Helper()
	start := time.Now()
	healthWait(t, within, func() bool { return r.settled(want) })
	return time.Since(start)
}

func (r *recoveryLab) ping(from, to string, id uint16) bool {
	send(r.nodes[from], packet(r.addr(from), r.addr(to), protoICMP, 64, id, 0, false, icmp(8, id, nil)))
	end := time.After(3 * time.Second)
	for {
		select {
		case p := <-r.nodes[to].ct.Inbound:
			if len(p) >= 28 && p[9] == protoICMP && p[20] == 8 && p[24] == byte(id>>8) && p[25] == byte(id) {
				return true
			}
		case <-end:
			return false
		}
	}
}

func (r *recoveryLab) reachable(t *testing.T, pairs [][2]string, id uint16) {
	t.Helper()
	for _, p := range pairs {
		if !r.ping(p[0], p[1], id) {
			t.Fatalf("%s -> %s not delivered", p[0], p[1])
		}
	}
}

// The controller sends a new revision only to the nodes whose config
// changed, so the member runs revision 2 against relays still on 1.
func TestHealthMemberUpdateKeepsEntry(t *testing.T) {
	r := newRecoveryLab(t, "A", "B")
	r.up()
	defer r.dump()
	onA := map[string]string{"M": "A", "N": "A"}
	r.waitSettled(t, onA, 20*time.Second)
	r.reachable(t, [][2]string{{"M", "N"}, {"N", "M"}}, 1)
	limit := 36.0
	r.upload["M"], r.revision["M"] = &limit, 2
	if err := r.nodes["M"].applyConfig(r.config("M")); err != nil {
		t.Fatal(err)
	}
	time.Sleep(8 * time.Second) // past the 6 s lease the relays gave M before
	r.waitSettled(t, onA, 2*time.Second)
	r.reachable(t, [][2]string{{"M", "N"}, {"N", "M"}, {"M", "B"}}, 2)
}

func TestHealthMemberRestartWithState(t *testing.T) {
	r := newRecoveryLab(t, "A", "B")
	r.up()
	defer r.dump()
	onA := map[string]string{"M": "A", "N": "A"}
	r.waitSettled(t, onA, 20*time.Second)
	r.revision["M"] = 2
	r.restart("M")
	t.Log("member back after", r.waitSettled(t, onA, 10*time.Second))
	r.reachable(t, [][2]string{{"M", "N"}, {"N", "M"}}, 3)
}

type tcpProxy struct {
	net.Listener
	mu    sync.Mutex
	conns []net.Conn
}

func newTCPProxy(t *testing.T, target string) *tcpProxy {
	l, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	p := &tcpProxy{Listener: l}
	t.Cleanup(func() { l.Close(); p.cut() })
	go func() {
		for {
			c, err := l.Accept()
			if err != nil {
				return
			}
			u, err := net.Dial("tcp4", target)
			if err != nil {
				c.Close()
				continue
			}
			p.mu.Lock()
			p.conns = append(p.conns, c, u)
			p.mu.Unlock()
			go func() { io.Copy(u, c); u.Close() }()
			go func() { io.Copy(c, u); c.Close() }()
		}
	}()
	return p
}

// cut closes every connection through the proxy and says how many there were.
func (p *tcpProxy) cut() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, c := range p.conns {
		c.Close()
	}
	n := len(p.conns) / 2
	p.conns = nil
	return n
}

func TestHealthMemberTCPReconnect(t *testing.T) {
	r := newRecoveryLab(t, "A", "B")
	proxy := newTCPProxy(t, fmt.Sprintf("127.0.0.1:%d", r.tcp["A"]))
	r.via["M/A"] = "tcp://" + proxy.Addr().String()
	r.up()
	defer r.dump()
	r.waitSettled(t, map[string]string{"M": "A", "N": "A"}, 20*time.Second)
	r.reachable(t, [][2]string{{"M", "N"}, {"N", "M"}}, 4)
	if proxy.cut() == 0 {
		t.Fatal("no connection to cut")
	}
	start := time.Now()
	healthWait(t, 15*time.Second, func() bool {
		s := r.nodes["M"].status()
		return s.Entry != nil && s.Entry.Transport != "" && r.nodes["M"].health.Snapshot().Error == "" && r.ping("M", "N", 5) && r.ping("N", "M", 6)
	})
	t.Log("member reachable again after", time.Since(start), "through", r.nodes["M"].status().Entry.Relay)
	t.Log("back on A over a new connection after", r.waitSettled(t, map[string]string{"M": "A"}, 30*time.Second))
	r.reachable(t, [][2]string{{"M", "N"}, {"N", "M"}}, 7)
}

// B accepts the relay link that A dials. While B restarts the members move
// to A and come back, so B has to tell A about the newer attachment.
func TestHealthRelayRestartRelinks(t *testing.T) {
	r := newRecoveryLab(t, "B", "A")
	r.up()
	defer r.dump()
	onB := map[string]string{"M": "B", "N": "B"}
	r.waitSettled(t, onB, 20*time.Second)
	r.reachable(t, [][2]string{{"M", "A"}, {"A", "M"}}, 8)
	r.nodes["B"].close()
	r.down["B"] = true
	healthWait(t, 15*time.Second, func() bool {
		for _, m := range []string{"M", "N"} {
			if s := r.nodes[m].status(); s.Entry == nil || s.Entry.Relay != "A" {
				return false
			}
		}
		return true
	})
	r.nodes["B"], r.down["B"] = r.startHealthNode("B"), false
	t.Log("members back on the restarted relay after", r.waitSettled(t, onB, 60*time.Second))
	r.reachable(t, [][2]string{{"M", "A"}, {"A", "M"}, {"N", "A"}, {"M", "B"}}, 9)
}
