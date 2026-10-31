// Package mesh is the OpenBear mesh data plane: a user-space WireGuard
// device that forwards between members on relays and filters what reaches
// this machine. openbear-node runs it as its own service ("mesh run").
package mesh

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"maps"
	"os"
	"path/filepath"
	"runtime/debug"
	"time"
)

// Run blocks until ctx ends, then removes the interface, routes and firewall
// rules it added.
func Run(ctx context.Context, dir string) error {
	started := time.Now()
	events := &eventLog{}
	r := &runner{dir: dir, started: started, events: events}
	keyRaw, err := os.ReadFile(filepath.Join(dir, "keys", "wg.key"))
	if err != nil {
		r.fail(0, err)
		return err
	}
	privateKey := string(bytes.TrimSpace(keyRaw))

	// Until a config can be brought up the service stays and reports why,
	// rather than exiting into a restart loop.
	var e *engine
	retry := time.NewTicker(time.Second)
	defer retry.Stop()
	var lastTry time.Time
	for e == nil {
		raw, info, err := r.read()
		if err == nil && (r.changed(raw, info) || time.Since(lastTry) > 10*time.Second) {
			r.remember(raw, info)
			lastTry = time.Now()
			var c *Config
			if c, err = ParseConfig(raw); err == nil {
				if e, err = bringUp(dir, c, privateKey, events); err == nil {
					limitMemory(c)
					r.revision, r.applied, r.err = c.Revision, c.Revision, ""
					events.add("config.applied", fmt.Sprintf("revision %d", c.Revision))
					r.saveApplied(raw)
				}
			}
			if err != nil {
				r.fail(revisionOf(raw), err)
				e = r.fallback(privateKey, raw, err)
			}
		} else if err != nil && !errors.Is(err, os.ErrNotExist) {
			r.fail(0, err)
		} else if err != nil {
			r.fail(0, errors.New("waiting for config.json"))
		}
		if e != nil {
			break
		}
		select {
		case <-ctx.Done():
			return nil
		case <-retry.C:
		}
	}
	defer func() {
		e.close()
		events.add("stop", "stopped")
		st := Status{ConfigRevision: r.revision, AppliedRevision: r.applied, ConfigStamp: r.stamp(), Error: r.err, Iface: e.name}
		r.write(st)
	}()

	tick := time.NewTicker(time.Second)
	defer tick.Stop()
	learned := map[string]string{}
	for n := 0; ; n++ {
		select {
		case <-ctx.Done():
			return nil
		case <-e.attach.Changes():
			if err := e.reroute(); err != nil {
				events.add("route.error", err.Error())
			}
			continue
		case <-tick.C:
		}
		r.reload(e)
		e.filter.expire()
		if n%5 == 0 {
			for _, fixed := range e.host.heal() {
				events.add("firewall.restored", fixed)
			}
			if err := e.recheckLocal(); err != nil {
				events.add("route.error", err.Error())
			}
		}
		if n%10 == 0 {
			if cur := e.learnedEndpoints(); !maps.Equal(cur, learned) {
				if err := saveEndpoints(dir, cur); err == nil {
					learned = cur
				}
			}
		}
		st := e.status()
		st.ConfigRevision, st.AppliedRevision, st.ConfigStamp = r.revision, r.applied, r.stamp()
		if r.err != "" {
			st.Error = r.err
		}
		r.write(st)
	}
}

// runner follows config.json and writes status.json.
type runner struct {
	dir      string
	started  time.Time
	events   *eventLog
	raw      []byte
	mod      time.Time
	size     int64
	revision int64 // of the config last read
	applied  int64
	err      string
	tried    time.Time
	cpu      cpuSampler
}

func (r *runner) read() ([]byte, os.FileInfo, error) {
	path := filepath.Join(r.dir, "config.json")
	info, err := os.Stat(path)
	if err != nil {
		return nil, nil, err
	}
	if !r.changed(nil, info) {
		return r.raw, info, nil
	}
	raw, err := os.ReadFile(path)
	return raw, info, err
}

// changed with nil raw compares only the file's size and time.
func (r *runner) changed(raw []byte, info os.FileInfo) bool {
	if !info.ModTime().Equal(r.mod) || info.Size() != r.size {
		return true
	}
	return raw != nil && !bytes.Equal(raw, r.raw)
}

func (r *runner) remember(raw []byte, info os.FileInfo) {
	r.raw, r.mod, r.size = raw, info.ModTime(), info.Size()
}

func (r *runner) stamp() int64 {
	if r.mod.IsZero() {
		return 0
	}
	return r.mod.UnixNano()
}

// reload applies a changed config, keeping the old one when the new one
// fails. A rewrite with the same content reloads the TLS certificate, or,
// while that content is not applied, tries it again: the controller's retry.
func (r *runner) reload(e *engine) {
	raw, info, err := r.read()
	if err != nil {
		return
	}
	rewritten := r.changed(nil, info)
	same := bytes.Equal(raw, r.raw)
	retry := r.err != "" && time.Since(r.tried) > 30*time.Second
	if !rewritten && !retry {
		return
	}
	r.remember(raw, info)
	if same && r.err == "" {
		if err := e.reloadCertificate(); err != nil {
			r.err = "reload certificate: " + err.Error()
			r.events.add("config.error", r.err)
		}
		return
	}
	r.tried = time.Now()
	r.revision = revisionOf(raw)
	c, err := ParseConfig(raw)
	if err == nil {
		err = e.applyConfig(c)
	}
	if err != nil {
		msg := fmt.Sprintf("revision %d not applied: %v", r.revision, err)
		if msg != r.err {
			r.events.add("config.error", msg)
			log.Printf("mesh: %s", msg)
		}
		r.err = msg
		return
	}
	limitMemory(c)
	r.applied, r.err = c.Revision, ""
	r.events.add("config.applied", fmt.Sprintf("revision %d", c.Revision))
	r.saveApplied(raw)
}

// appliedFile keeps the config last applied. A running data plane keeps it
// when a new revision fails (a port another program holds); after a restart
// with that revision still in config.json it is brought up instead of
// nothing, and reload keeps trying the new one.
const appliedFile = "applied.json"

func (r *runner) saveApplied(raw []byte) {
	path := filepath.Join(r.dir, appliedFile)
	if old, err := os.ReadFile(path); err == nil && bytes.Equal(old, raw) {
		return
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, raw, 0o600); err != nil {
		log.Printf("mesh: %s: %v", appliedFile, err)
		return
	}
	if err := os.Rename(tmp, path); err != nil {
		os.Remove(tmp)
		log.Printf("mesh: %s: %v", appliedFile, err)
	}
}

// fallback brings up the config last applied when config.json, failed,
// cannot be; nil when there is none or it cannot either.
func (r *runner) fallback(privateKey string, failed []byte, cause error) *engine {
	raw, err := os.ReadFile(filepath.Join(r.dir, appliedFile))
	if err != nil || bytes.Equal(raw, failed) {
		return nil
	}
	c, err := ParseConfig(raw)
	if err != nil {
		return nil
	}
	e, err := bringUp(r.dir, c, privateKey, r.events)
	if err != nil {
		return nil
	}
	limitMemory(c)
	r.applied, r.tried = c.Revision, time.Now()
	r.err = fmt.Sprintf("revision %d not applied: %v", r.revision, cause)
	r.events.add("config.applied", fmt.Sprintf("revision %d, the last applied; %s", c.Revision, r.err))
	log.Printf("mesh: running revision %d, the last applied; %s", c.Revision, r.err)
	return e
}

// limitMemory makes the GC work harder before the process reaches the
// controller's alarm level: 160 MiB for members, 256 MiB for relays.
// GOMEMLIMIT, when set, wins.
func limitMemory(c *Config) {
	if os.Getenv("GOMEMLIMIT") != "" {
		return
	}
	limit := int64(128 << 20)
	if resourceProfile(c) == roleRelay {
		limit = 208 << 20
	}
	debug.SetMemoryLimit(limit)
}

func (r *runner) fail(revision int64, err error) {
	if revision != 0 {
		r.revision = revision
	}
	if r.err != err.Error() {
		r.events.add("config.error", err.Error())
		log.Printf("mesh: %v", err)
	}
	r.err = err.Error()
	r.write(Status{ConfigRevision: r.revision, AppliedRevision: r.applied, ConfigStamp: r.stamp(), Error: r.err})
}

func (r *runner) write(s Status) {
	fillProcess(&s, r.started, r.events)
	used, ok := processCPU()
	s.Resources.CPUPercent = r.cpu.percent(time.Now(), used, ok)
	if err := writeAtomic(filepath.Join(r.dir, "status.json"), s); err != nil {
		log.Printf("mesh: status: %v", err)
	}
}

func revisionOf(raw []byte) int64 {
	var head struct {
		Revision int64 `json:"revision"`
	}
	json.Unmarshal(raw, &head)
	return head.Revision
}

// bringUp creates the interface and starts the data plane on it.
func bringUp(dir string, c *Config, privateKey string, events *eventLog) (*engine, error) {
	dev, err := createTUN(dir, c.Network.MTU-e2eOverhead)
	if err != nil {
		return nil, fmt.Errorf("create interface: %w", err)
	}
	name, err := dev.Name()
	if err != nil {
		dev.Close()
		return nil, err
	}
	return startEngine(dir, c, privateKey, dev, newHost(name, dir, dev), NewAttachment(c), events)
}
