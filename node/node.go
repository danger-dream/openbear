package node

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"math/rand/v2"
	"net"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const (
	closeUnauthorized = 4401
	closeRevoked      = 4403
	closeReplaced     = 4409
	// closeProtocol: the controller does not speak any protocol in this
	// node's range (ProtocolMin..ProtocolVersion). The close reason says why.
	closeProtocol = 4426
	// Commands and file transfers running at once; more are refused, never queued.
	maxBusyCalls = 8
)

// homeProbeInterval is how often a node on its backup address checks the primary.
var homeProbeInterval = 10 * time.Minute

// idlePoll: how often a move waiting for running calls and terminals looks again.
var idlePoll = time.Second

// A controller that answers it is still starting is retried on the same
// address every startingRetry plus up to 1.5 times that, without counting as a
// failed attempt, until it has said so for startingGrace.
var (
	startingRetry = 2 * time.Second
	startingGrace = time.Minute
)

// An address is left for the other one once it has failed for switchAfter,
// at least twice; until then it is retried at least every failingRetry, so a
// controller that restarts is back within seconds of answering again. The end
// of a session that ran for sessionSettled is no failure.
var (
	switchAfter    = 45 * time.Second
	failingRetry   = 5 * time.Second
	sessionSettled = time.Minute
)

var (
	errRevoked      = errors.New("node was revoked by the controller")
	errUnauthorized = errors.New("controller rejected this node's credentials")
	// ErrRestart asks main to exec the binary that an update just installed.
	ErrRestart = errors.New("restart into the updated binary")
	// ErrUninstalled asks main to remove this node's files and exit.
	ErrUninstalled = errors.New("controller asked to uninstall this node")
	errReconnect   = errors.New("reconnect to the new controller address")
)

// protocolError is a controller refusing this node's protocol range.
type protocolError struct{ reason string }

func (e *protocolError) Error() string {
	msg := fmt.Sprintf("the controller does not speak this node's protocol %d..%d", ProtocolMin, ProtocolVersion)
	if e.reason != "" {
		msg += " (" + e.reason + ")"
	}
	return msg
}

// notice is what Run makes of a refusal the controller authenticated: signed
// for this handshake, or sent inside the channel. A bare close code may come
// from anything on the path, so only a notice makes the node wait long.
func notice(code int, reason string) error {
	switch code {
	case closeRevoked:
		return errRevoked
	case closeProtocol:
		return &protocolError{reason}
	case closeUnauthorized:
		return errUnauthorized
	}
	return &CloseError{Code: code, Reason: reason}
}

// plainRefusal: a revocation, protocol or credentials refusal that is not a
// notice, as a controller from before notices sends them.
func plainRefusal(err error) bool {
	var closeErr *CloseError
	var handshake *HandshakeError
	if errors.As(err, &closeErr) {
		return closeErr.Code == closeRevoked || closeErr.Code == closeProtocol || closeErr.Code == closeUnauthorized
	}
	return errors.As(err, &handshake) && handshake.Status == http.StatusUnauthorized
}

// controllerStarting: the controller is up but not yet serving, which it says
// to every request until its startup finishes.
func controllerStarting(err error) bool {
	var handshake *HandshakeError
	if !errors.As(err, &handshake) || handshake.Status != http.StatusServiceUnavailable {
		return false
	}
	var body struct {
		Error string `json:"error"`
	}
	return json.Unmarshal(handshake.Body, &body) == nil && body.Error == "service_not_ready"
}

// handshakeParams is what the node sends with its signature: its version, the
// protocol range it speaks, and that it takes refusals as notices.
func handshakeParams(probe bool) map[string]any {
	m := map[string]any{"version": Version, "protocol": ProtocolVersion, "protocolMin": ProtocolMin, "notices": true}
	if probe {
		m["probe"] = true
	}
	return m
}

// commonProtocol is the highest protocol in both this node's range and
// low..high; false when they do not overlap.
func commonProtocol(low, high int) (int, bool) {
	if low <= 0 || low > high {
		low = high
	}
	p := min(high, ProtocolVersion)
	return p, p >= max(low, ProtocolMin)
}

type Agent struct {
	Config     Config
	ConfigPath string
	Collector  *Collector
	Logger     *log.Logger

	mu     sync.Mutex // Config URLs, the address in use and the current session
	cancel context.CancelFunc
	next   error // what Run does after the session that a call ended
	backup bool  // connected through Config.Backup
	ch     *channel
	// handover is closed once the next session said hello (leaveMesh).
	handover chan struct{}
	// held counts operations taking the mesh down; meanwhile no session
	// goes back to a mesh address.
	held int

	linkOnce sync.Once
	link     meshLink // tests set their own; nil where there is no mesh

	ipMu       sync.Mutex
	ipv4, ipv6 string
	ipv4Proxy  string
	ipAt       time.Time

	busy    chan struct{}
	uploads *uploads
	exe     string // replaced by updates; tests point it elsewhere

	// active counts the calls and terminals running on the current session.
	active atomic.Int64

	buildOnce sync.Once
	buildHash string
	updateMu  sync.Mutex

	browserOnce sync.Once
	browser     *browserHost

	jobsOnce sync.Once
	jobStore *jobStore
	jobDir   string // tests point it elsewhere

	meshOnce sync.Once
	meshHost *meshHost // tests set their own

	probeOnce sync.Once
	probes    *probeManager // tests set their own

	ddns ddnsWatcher
}

type envelope struct {
	T        string          `json:"t"`
	ID       string          `json:"id,omitempty"`
	Method   string          `json:"method,omitempty"`
	Params   json.RawMessage `json:"params,omitempty"`
	Nonce    string          `json:"nonce,omitempty"`
	Eph      string          `json:"eph,omitempty"`
	Sig      string          `json:"sig,omitempty"`
	Interval float64         `json:"interval,omitempty"`
	Sid      uint32          `json:"sid,omitempty"`
	Cols     int             `json:"cols,omitempty"`
	Rows     int             `json:"rows,omitempty"`
	N        int             `json:"n,omitempty"`
	// Protocol and ProtocolMin, in the session config, are the controller's
	// range; a controller from before them sends neither.
	Protocol    int `json:"protocol,omitempty"`
	ProtocolMin int `json:"protocolMin,omitempty"`
	// Code and Reason, in a close notice, are a close code and its reason.
	Code   int    `json:"code,omitempty"`
	Reason string `json:"reason,omitempty"`
	// ParamsSig, in the welcome, is the controller's signature over this
	// node's signed handshake parameters; SignedParams, in the session config,
	// whether it went by them. A controller from before them sends neither.
	ParamsSig    string `json:"paramsSig,omitempty"`
	SignedParams *bool  `json:"signedParams,omitempty"`
}

func NewAgent(c Config, logger *log.Logger) *Agent {
	return &Agent{Config: c, Collector: NewCollector(), Logger: logger, busy: make(chan struct{}, maxBusyCalls), uploads: newUploads()}
}

func (a *Agent) config() Config {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.Config
}

// finish ends the current session after the reply that requested it is sent.
func (a *Agent) finish(next error) {
	a.mu.Lock()
	a.next = next
	cancel := a.cancel
	a.mu.Unlock()
	if cancel != nil {
		cancel()
	}
}

// Run keeps one connection to the controller alive until ctx ends, an update
// needs a restart, or the controller uninstalls this node. An address that
// keeps failing (see switchAfter) moves to the other one, if configured; a
// controller that is starting is waited for on the same address first.
func (a *Agent) Run(ctx context.Context) error {
	a.build() // read before connecting, so it never delays the hello
	a.resumeMeshChange()
	backoff := time.Second
	var lastErr string
	var lastLog time.Time
	// failures in a row on the address in use, the first of them at failingSince.
	failures := 0
	var failingSince time.Time
	var lastSkip string
	var lastSkipLog time.Time
	var startingSince time.Time
	for {
		if why := a.skipDownMesh(); why != "" {
			failures, failingSince = 0, time.Time{}
			if why != lastSkip || time.Since(lastSkipLog) > 10*time.Minute {
				a.Logger.Printf("%s; using the other address", why)
				lastSkip, lastSkipLog = why, time.Now()
			}
		}
		started := time.Now()
		err := a.session(ctx)
		if ctx.Err() != nil {
			return nil
		}
		a.mu.Lock()
		next := a.next
		a.next = nil
		a.mu.Unlock()
		switch next {
		case ErrUninstalled:
			if a.browser != nil {
				a.browser.stop()
			}
			return next
		case ErrRestart:
			return next
		case errReconnect:
			backoff, failures, failingSince = time.Second, 0, time.Time{}
			continue
		}
		ran := time.Since(started) > sessionSettled
		if ran {
			backoff, failures, failingSince = time.Second, 0, time.Time{}
		}
		if !controllerStarting(err) {
			startingSince = time.Time{}
		} else if startingSince.IsZero() {
			startingSince = time.Now()
		}
		if !startingSince.IsZero() && time.Since(startingSince) < startingGrace {
			if message := "the controller is starting"; message != lastErr || time.Since(lastLog) > 10*time.Minute {
				a.Logger.Printf("%s; retrying shortly", message)
				lastErr, lastLog = message, time.Now()
			}
			select {
			case <-ctx.Done():
				return nil
			case <-time.After(startingRetry + time.Duration(rand.Int64N(int64(startingRetry*3/2)+1))):
			}
			continue
		}
		// An address that sent a refusal anyone on the path could have sent or
		// that tampered with the handshake is left after it. A welcome that does
		// not verify is one of them: it proves nothing, so it waits no longer
		// than a disconnect.
		plain := plainRefusal(err) || errors.Is(err, errParamsStripped) || errors.Is(err, errControllerIdentity)
		var protocol *protocolError
		refused := plain || errors.Is(err, errRevoked) || errors.As(err, &protocol) || errors.Is(err, errUnauthorized)
		if refused || !ran {
			failures++
			if failingSince.IsZero() {
				// A controller that stayed starting has been failing since then.
				failingSince = time.Now()
				if !startingSince.IsZero() {
					failingSince = startingSince
				}
			}
		}
		var failing time.Duration
		if !failingSince.IsZero() {
			failing = time.Since(failingSince)
		}
		if (plain || failures >= 2 && failing >= switchAfter) && a.switchAddress() {
			failures, failingSince, failing = 0, time.Time{}, 0
			// Both addresses refusing so must not mean a retry every second.
			if !plain {
				backoff = time.Second
			}
		}
		wait := backoff
		message := ""
		switch {
		case errors.Is(err, errRevoked):
			message, wait = "controller revoked this node; re-enroll to connect again", time.Hour
		case errors.As(err, &protocol):
			// Only an update of this node or of the controller changes that.
			message, wait = protocol.Error()+"; update this node or the controller", 10*time.Minute
		case errors.Is(err, errUnauthorized):
			message, wait = errUnauthorized.Error(), 5*time.Minute
		default:
			message = "disconnected: " + err.Error()
			backoff = min(backoff*2, time.Minute)
			if !plain && failing < switchAfter {
				backoff = min(backoff, failingRetry)
			}
		}
		// A controller that stays down must not fill the journal.
		if message != lastErr || time.Since(lastLog) > 10*time.Minute {
			a.Logger.Printf("%s; retrying in %s", message, wait.Round(time.Second))
			lastErr, lastLog = message, time.Now()
		}
		wait += time.Duration(rand.Int64N(int64(wait/4) + 1))
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(wait):
		}
	}
}

func (a *Agent) dial(ctx context.Context, c Config, hello map[string]any) (*Conn, *channel, envelope, error) {
	var none envelope
	base, err := ControllerURL(c.URL)
	if err != nil {
		return nil, nil, none, err
	}
	key, err := c.Key()
	if err != nil {
		return nil, nil, none, err
	}
	controller, err := c.Controller()
	if err != nil {
		return nil, nil, none, err
	}
	nonce := upgradeNonce()
	header := http.Header{"User-Agent": {"openbear-node/" + Version}, "X-Openbear-Node": {c.NodeID}, "X-Openbear-Nonce": {nonce}}
	timeout := 20 * time.Second
	if a.overMesh(c.URL) {
		timeout = meshDialTimeout
	}
	conn, err := Dial(ctx, endpoint(base, "/api/server-nodes/v1/connect", true), header, timeout)
	if err != nil {
		return nil, nil, none, signedUpgradeRefusal(err, controller, c.NodeID, nonce)
	}
	conn.SetIdleTimeout(20 * time.Second)
	ch, first, err := secure(conn, c.NodeID, key, controller, hello)
	if err != nil {
		conn.raw.Close()
		return nil, nil, none, err
	}
	if first.T == "close" {
		conn.raw.Close()
		return nil, nil, none, notice(first.Code, first.Reason)
	}
	return conn, ch, first, nil
}

// switchAddress flips between the primary and backup address; false when
// there is no backup.
func (a *Agent) switchAddress() bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.Config.Backup == "" {
		a.backup = false
		return false
	}
	a.backup = !a.backup
	which := "primary"
	if a.backup {
		which = "backup"
	}
	a.Logger.Printf("switching to the %s address", which)
	return true
}

// current is the config as used for the next connection: URL is the address in use.
func (a *Agent) current() (Config, bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	c := a.Config
	if a.backup && c.Backup != "" {
		c.URL = c.Backup
		return c, true
	}
	return c, false
}

// returnHome probes the primary address while connected through the backup
// and reconnects there once it answers.
func (a *Agent) returnHome(ctx context.Context, every time.Duration) {
	if a.meshExit(false) {
		a.returnHomeOverMesh(ctx)
		return
	}
	ticker := time.NewTicker(every)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
		if !a.primaryAnswers(ctx) {
			continue
		}
		if a.active.Load() > 0 {
			if !a.untilIdle(ctx) {
				return
			}
			if !a.primaryAnswers(ctx) {
				continue
			}
		}
		a.mu.Lock()
		a.backup = false
		a.mu.Unlock()
		a.Logger.Print("primary address is reachable again; moving back")
		a.finish(errReconnect)
		return
	}
}

func (a *Agent) primaryAnswers(ctx context.Context) bool {
	conn, _, first, err := a.dial(ctx, a.config(), handshakeParams(true))
	if err != nil {
		return false
	}
	conn.Close()
	return first.T == "config"
}

// untilIdle waits for the calls and terminals on this session to end: a move
// this node makes on its own ends the session, and with it what runs there.
// It is false once ctx ends.
func (a *Agent) untilIdle(ctx context.Context) bool {
	ticker := time.NewTicker(idlePoll)
	defer ticker.Stop()
	for a.active.Load() > 0 {
		select {
		case <-ctx.Done():
			return false
		case <-ticker.C:
		}
	}
	return ctx.Err() == nil
}

func (a *Agent) session(parent context.Context) error {
	c, onBackup := a.current()
	conn, ch, first, err := a.dial(parent, c, handshakeParams(false))
	if err != nil {
		return err
	}
	ctx, cancel := context.WithCancel(parent)
	defer cancel()
	a.mu.Lock()
	a.cancel = cancel
	a.mu.Unlock()
	go func() { <-ctx.Done(); conn.Close() }()
	if first.T != "config" {
		return errors.New("controller sent no session config")
	}
	interval := time.Duration(max(1, min(first.Interval, 60)) * float64(time.Second))
	a.Logger.Printf("connected to %s as %s", conn.raw.RemoteAddr(), c.NodeID)
	// A controller that sees no common protocol may keep the session, so it
	// can still update this node; it decides what it calls.
	if first.Protocol > 0 {
		if _, ok := commonProtocol(first.ProtocolMin, first.Protocol); !ok {
			a.Logger.Printf("the controller speaks protocol %d..%d and this node %d..%d: no protocol in common; update this node or the controller",
				max(first.ProtocolMin, 1), first.Protocol, ProtocolMin, ProtocolVersion)
		}
	}
	// The controller pings every 25 seconds; silence beyond that is a dead link.
	conn.SetIdleTimeout(75 * time.Second)
	if err = a.sendHello(ch, c.URL, onBackup); err != nil {
		return err
	}
	a.sessionUp(ch)
	defer a.sessionDown(ch)
	if onBackup {
		go a.returnHome(ctx, homeProbeInterval)
	}
	if a.meshExit(onBackup) {
		go a.watchMesh(ctx, onBackup)
	}
	go a.refreshIPs(ctx, ch, false)
	// The next session, which may follow at once when the address changes,
	// samples the same Collector: this one's reports end first.
	reported := make(chan struct{})
	go func() { a.report(ctx, ch, interval); close(reported) }()
	defer func() { cancel(); <-reported }()
	ts := newTerminals()
	defer ts.closeAll()
	for {
		kind, data, err := ch.read()
		if err != nil {
			if ctx.Err() != nil && parent.Err() == nil {
				return errors.New("connection lost")
			}
			return err
		}
		if kind == kindTerminal {
			ts.input(data)
			continue
		}
		var message envelope
		if kind != kindJSON || json.Unmarshal(data, &message) != nil {
			continue
		}
		switch message.T {
		case "close":
			return notice(message.Code, message.Reason)
		case "call":
			go a.call(ctx, ch, message)
		case "term.open":
			a.termOpen(ctx, ch, ts, message)
		case "term.resize":
			if t := ts.get(message.Sid); t != nil {
				t.resize(message.Cols, message.Rows)
			}
		case "term.ack":
			if t := ts.get(message.Sid); t != nil {
				t.ack(message.N)
			}
		case "term.close":
			if t := ts.get(message.Sid); t != nil {
				t.stop("closed")
			}
		}
	}
}

func (a *Agent) sendHello(ch *channel, address string, onBackup bool) error {
	info := a.Collector.Info()
	a.ipMu.Lock()
	info.IPv4, info.IPv6, info.IPv4Proxy = a.ipv4, a.ipv6, a.ipv4Proxy
	a.ipMu.Unlock()
	return ch.send(map[string]any{"t": "hello", "info": info, "caps": a.config().Capabilities, "version": Version,
		"build": a.build(), "endpoint": address, "backup": onBackup, "features": Features,
		"protocol": ProtocolVersion, "protocolMin": ProtocolMin})
}

// refreshIPs looks up the public addresses off the connection path and sends a
// new hello only when they changed.
func (a *Agent) refreshIPs(ctx context.Context, ch *channel, force bool) {
	a.ipMu.Lock()
	fresh := !a.ipAt.IsZero() && !force
	old4, old6, oldProxy := a.ipv4, a.ipv6, a.ipv4Proxy
	a.ipMu.Unlock()
	if fresh {
		return
	}
	if v4, v6, proxy := a.publicIPs(ctx); v4 != old4 || v6 != old6 || proxy != oldProxy {
		c, onBackup := a.current()
		a.sendHello(ch, c.URL, onBackup)
	}
}

func (a *Agent) report(ctx context.Context, ch *channel, interval time.Duration) {
	a.Collector.Sample() // prime the CPU and network deltas
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	refresh := time.NewTicker(6 * time.Hour)
	defer refresh.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-refresh.C:
			go a.refreshIPs(ctx, ch, true)
		case <-ticker.C:
			report := map[string]any{"t": "report", "m": a.Collector.Sample()}
			sent := a.addMeshReport(report)
			if ch.send(report) != nil {
				ch.conn.Close()
				return
			}
			sent()
		}
	}
}

func (a *Agent) call(ctx context.Context, ch *channel, m envelope) {
	a.active.Add(1)
	defer a.active.Add(-1)
	reply := map[string]any{"t": "reply", "id": m.ID}
	state := &callState{}
	result, next, err := a.dispatch(context.WithValue(ctx, callKey{}, state), m)
	if err != nil {
		reply["ok"], reply["error"] = false, err.Error()
	} else {
		reply["ok"], reply["result"] = true, result
	}
	if state.handedOver.Load() {
		// The session it came on was ended to move off the mesh first.
		if cur := a.currentChannel(); cur != nil && cur != ch {
			ch = cur
		}
	}
	if ch.send(reply) == nil && next != nil {
		a.finish(next)
	}
	if m.Method == "browser.config" && err == nil {
		c, onBackup := a.current()
		a.sendHello(ch, c.URL, onBackup)
	}
}

func (a *Agent) dispatch(ctx context.Context, m envelope) (result any, next error, err error) {
	defer func() {
		if r := recover(); r != nil {
			a.Logger.Printf("call %s failed: %v", m.Method, r)
			result, next, err = nil, nil, errors.New("internal_error")
		}
	}()
	switch m.Method {
	case "ping":
		return map[string]any{"version": Version}, nil, nil
	case "uninstall":
		return map[string]any{"version": Version}, ErrUninstalled, nil
	case "config.url":
		result, err = a.changeURL(ctx, m.Params)
		if err == nil && result.(map[string]any)["changed"] == true {
			next = errReconnect
		}
		return result, next, err
	case "http.local":
		result, err = a.httpLocal(ctx, m.Params)
		return result, nil, err
	}
	if strings.HasPrefix(m.Method, "browser.") {
		result, err = a.browserCall(ctx, m)
		return result, nil, err
	}
	// Network tests only look outward, so they need no consent and run in the background.
	if strings.HasPrefix(m.Method, "net.") {
		result, err = a.netCall(ctx, m)
		return result, nil, err
	}
	// Address probes look outward too; a command probe checks consent when it runs.
	if strings.HasPrefix(m.Method, "ddns.") {
		result, err = a.ddnsCall(ctx, m)
		return result, nil, err
	}
	// Updating replaces the program that runs commands, so it needs the same consent.
	if !a.config().Capabilities.Exec {
		switch m.Method {
		case "exec", "update", "fs.list", "fs.stat", "fs.read", "fs.write", "fs.edit", "fs.transfer", "fs.hash", "fs.cancel",
			"job.start", "job.list", "job.status", "job.stop",
			"mesh.status", "mesh.install", "mesh.apply", "mesh.tls", "mesh.upgrade", "mesh.uninstall",
			"mesh.prepare", "mesh.activate", "mesh.confirm", "mesh.abort":
			return nil, nil, errors.New("capability_disabled")
		}
		return nil, nil, errors.New("unsupported_method")
	}
	select {
	case a.busy <- struct{}{}:
		defer func() { <-a.busy }()
	default:
		return nil, nil, errors.New("node_busy")
	}
	switch m.Method {
	case "exec":
		result, err = Exec(ctx, m.Params)
	case "update":
		result, err = a.update(ctx, m.Params)
		if err == nil && result.(map[string]any)["changed"] == true {
			next = ErrRestart
		}
	case "fs.list", "fs.stat", "fs.read", "fs.write", "fs.transfer", "fs.hash", "fs.cancel":
		result, err = a.uploads.handle(m.Method, m.Params)
	case "fs.edit":
		result, err = a.uploads.edit(m.Params)
	case "job.start", "job.list", "job.status", "job.stop":
		result, err = a.jobs().handle(m.Method, m.Params)
	case "mesh.status", "mesh.install", "mesh.apply", "mesh.tls", "mesh.upgrade", "mesh.uninstall",
		"mesh.prepare", "mesh.activate", "mesh.confirm", "mesh.abort":
		result, err = a.meshCall(ctx, m.Method, m.Params)
	default:
		err = errors.New("unsupported_method")
	}
	return result, next, err
}

// browserCall serves the browser methods. Turning the browser on needs
// command execution, which already gives the controller this machine.
func (a *Agent) browserCall(ctx context.Context, m envelope) (any, error) {
	h := a.browserHost()
	switch m.Method {
	case "browser.config":
		if !a.config().Capabilities.Exec {
			return nil, errors.New("capability_disabled")
		}
		return a.configureBrowser(m.Params)
	case "browser.status":
		return h.status(), nil
	}
	if !a.config().Capabilities.Browser {
		return nil, errors.New("capability_disabled")
	}
	switch m.Method {
	case "browser.start":
		return h.ensure(ctx)
	case "browser.call":
		return h.call(ctx, m.Params)
	case "browser.cdp":
		return h.cdp(ctx, m.Params)
	case "browser.artifact":
		return h.artifact(m.Params)
	case "browser.upload":
		return h.upload(m.Params)
	case "browser.restart":
		h.stop()
		status, err := h.ensure(ctx)
		// Nothing was attached yet and ensure took over a leftover window: restart that one too.
		if err == nil && h.reused() {
			h.stop()
			return h.ensure(ctx)
		}
		return status, err
	}
	return nil, errors.New("unsupported_method")
}

// changeURL switches addresses only after a full handshake proves the new
// primary reaches this same controller, so a typo cannot strand the node. The
// backup is checked too, but an unreachable backup is still saved.
func (a *Agent) changeURL(ctx context.Context, raw json.RawMessage) (map[string]any, error) {
	release, err := a.lockMeshAddresses()
	if err != nil {
		return nil, err
	}
	defer release()
	var p struct {
		URL    string `json:"url"`
		Backup string `json:"backup"`
	}
	if json.Unmarshal(raw, &p) != nil {
		return nil, errors.New("invalid_params")
	}
	base, err := ControllerURL(p.URL)
	if err != nil {
		return nil, errors.New("invalid_url")
	}
	backup := ""
	if p.Backup != "" {
		spare, err := ControllerURL(p.Backup)
		if err != nil {
			return nil, errors.New("invalid_url")
		}
		backup = spare.String()
	}
	c := a.config()
	if base.String() == c.URL && backup == c.Backup {
		return map[string]any{"url": c.URL, "backup": c.Backup, "changed": false}, nil
	}
	trial := c
	trial.URL, trial.Backup = base.String(), backup
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	reachable := func(address string) bool {
		probe := trial
		probe.URL = address
		conn, _, first, err := a.dial(ctx, probe, handshakeParams(true))
		if err != nil {
			return false
		}
		conn.Close()
		return first.T == "config"
	}
	if !reachable(trial.URL) {
		return nil, errors.New("new_url_unreachable")
	}
	backupOK := backup == "" || reachable(backup)
	if a.ConfigPath != "" {
		if err = SaveConfig(a.ConfigPath, trial); err != nil {
			return nil, errors.New("config_write_failed")
		}
	}
	a.mu.Lock()
	a.Config.URL, a.Config.Backup, a.backup = trial.URL, trial.Backup, false
	a.mu.Unlock()
	return map[string]any{"url": trial.URL, "backup": trial.Backup, "backupReachable": backupOK, "changed": true}, nil
}

// Echo services abroad and in China: behind a proxy that sends traffic abroad another
// way, the ones in China see the machine's own line and the ones abroad the proxy's exit.
var (
	publicV4Abroad = []string{"https://api.ip.sb/ip", "https://4.icanhazip.com"}
	publicV4China  = []string{"https://myip.ipip.net/", "https://ip.3322.net", "https://ddns.oray.com/checkip"}
	publicV6       = []string{"https://api.ip.sb/ip", "https://6.icanhazip.com"}
)

// publicIPs asks external echo services once per family; failures keep the
// previous answer so a flaky lookup does not blank the address.
func (a *Agent) publicIPs(ctx context.Context) (string, string, string) {
	var abroad, china, v6 string
	var wg sync.WaitGroup
	wg.Add(3)
	go func() { defer wg.Done(); abroad = lookupIP(ctx, "tcp4", publicV4Abroad) }()
	go func() { defer wg.Done(); china = lookupIP(ctx, "tcp4", publicV4China) }()
	go func() { defer wg.Done(); v6 = lookupIP(ctx, "tcp6", publicV6) }()
	wg.Wait()
	v4, proxy := pickPublicV4(china, abroad)
	a.ipMu.Lock()
	defer a.ipMu.Unlock()
	if v4 != "" || a.ipAt.IsZero() {
		a.ipv4, a.ipv4Proxy = v4, proxy
	}
	if v6 != "" || a.ipAt.IsZero() {
		a.ipv6 = v6
	}
	a.ipAt = time.Now()
	return a.ipv4, a.ipv6, a.ipv4Proxy
}

// pickPublicV4: the machine's IPv4, and the exit traffic abroad leaves from when the
// services in China and abroad disagree (the controller swaps them for a server its
// profile puts outside China).
func pickPublicV4(china, abroad string) (string, string) {
	switch {
	case china == "":
		return abroad, ""
	case abroad == "" || abroad == china:
		return china, ""
	}
	return china, abroad
}

func lookupIP(ctx context.Context, network string, urls []string) string {
	dialer := &net.Dialer{Timeout: 6 * time.Second}
	transport := &http.Transport{
		DialContext: func(ctx context.Context, _, addr string) (net.Conn, error) {
			return dialer.DialContext(ctx, network, addr)
		},
	}
	defer transport.CloseIdleConnections()
	client := &http.Client{Timeout: 8 * time.Second, Transport: transport}
	for _, u := range urls {
		req, _ := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
		req.Header.Set("User-Agent", "curl/8.4.0")
		resp, err := client.Do(req)
		if err != nil {
			continue
		}
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		resp.Body.Close()
		if addr, ok := ddnsParse(string(body), network == "tcp6"); resp.StatusCode == http.StatusOK && ok {
			return addr.String()
		}
	}
	return ""
}
