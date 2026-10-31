package node

import (
	"context"
	"sync/atomic"
	"time"
)

// The control connection may go to the controller's mesh address. When the
// address in use goes through this machine's mesh and the other configured
// address does not, the node does not wait 75 seconds for the link to die
// when the mesh here goes down: it leaves before an operation of its own
// takes the mesh down, leaves at once when the data plane stops being up,
// skips the mesh address while the mesh is down, and from the other address
// returns as soon as the mesh is back, checking every 30 seconds after that.
// With the mesh address alone it waits for the mesh as before.

// meshLink is what the control connection needs to know of the mesh here.
type meshLink interface {
	// carries: address's host is an IP inside the network prefix of the
	// config the data plane last applied.
	carries(address string) bool
	// up: the data plane runs, keeps its status fresh and is ready, its
	// interface present; otherwise why not.
	up() (bool, string)
}

var (
	// meshWatchInterval is how often the mesh is checked while it matters.
	meshWatchInterval = time.Second
	// meshHomeProbeInterval replaces homeProbeInterval when the primary is a
	// mesh address: how often it is tried while the mesh is up.
	meshHomeProbeInterval = 30 * time.Second
	// For meshHomeBurst after the mesh came up the primary is tried every
	// meshHomeRetry: the controller may answer a moment after the mesh does.
	meshHomeRetry = 3 * time.Second
	meshHomeBurst = 15 * time.Second
	// meshDialTimeout bounds connecting to a mesh address; others get 20 seconds.
	meshDialTimeout = 8 * time.Second
	// handoverWait is how long an operation waits for the session on the
	// other address before it goes ahead regardless.
	handoverWait = 30 * time.Second
)

func (a *Agent) meshLink() meshLink {
	a.linkOnce.Do(func() {
		if a.link == nil {
			a.link = a.platformMeshLink()
		}
	})
	return a.link
}

// overMesh: address goes through this machine's mesh.
func (a *Agent) overMesh(address string) bool {
	l := a.meshLink()
	return l != nil && address != "" && l.carries(address)
}

// meshExit: the address in use (the backup when onBackup) goes through the
// mesh and the other configured one does not, so there is somewhere to go
// when the mesh is down.
func (a *Agent) meshExit(onBackup bool) bool {
	c := a.config()
	in, other := c.URL, c.Backup
	if onBackup {
		in, other = c.Backup, c.URL
	}
	return other != "" && a.overMesh(in) && !a.overMesh(other)
}

func (a *Agent) onBackup() bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.backup && a.Config.Backup != ""
}

// moveTo makes the next connection use the backup or the primary address.
func (a *Agent) moveTo(backup bool, why string) {
	a.mu.Lock()
	a.backup = backup && a.Config.Backup != ""
	which := "primary"
	if a.backup {
		which = "backup"
	}
	a.mu.Unlock()
	a.Logger.Printf("%s; switching to the %s address", why, which)
}

// skipDownMesh, before connecting: when the address in use goes through the
// mesh, the mesh here is down and the other address does not need it, the
// next connection uses the other one without trying. It returns why, or "".
func (a *Agent) skipDownMesh() string {
	onBackup := a.onBackup()
	if !a.meshExit(onBackup) {
		return ""
	}
	up, why := a.meshLink().up()
	if up {
		return ""
	}
	a.mu.Lock()
	a.backup = !onBackup
	a.mu.Unlock()
	return "the mesh here is down (" + why + ")"
}

// watchMesh runs with a session on a mesh address that has a way out: once
// the mesh here is not up the session ends and the next uses the other address.
func (a *Agent) watchMesh(ctx context.Context, onBackup bool) {
	ticker := time.NewTicker(meshWatchInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
		if !a.meshExit(onBackup) {
			continue
		}
		if up, why := a.meshLink().up(); !up && ctx.Err() == nil {
			a.moveTo(!onBackup, "the mesh here is down ("+why+")")
			a.finish(errReconnect)
			return
		}
	}
}

// meshUsable: the mesh is up and no operation of this node is taking it down.
func (a *Agent) meshUsable() bool {
	a.mu.Lock()
	held := a.held > 0
	a.mu.Unlock()
	if held {
		return false
	}
	up, _ := a.meshLink().up()
	return up
}

// returnHomeOverMesh is returnHome for a primary mesh address: the primary
// is tried as soon as the mesh here is up, a few times shortly after, then
// every meshHomeProbeInterval while it stays up; never while it is down.
func (a *Agent) returnHomeOverMesh(ctx context.Context) {
	ticker := time.NewTicker(meshWatchInterval)
	defer ticker.Stop()
	wasUp := a.meshUsable()
	last := time.Now()
	var burst time.Time
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
		up, now := a.meshUsable(), time.Now()
		due := false
		switch {
		case !up:
		case !wasUp:
			due, burst = true, now.Add(meshHomeBurst)
		case now.Before(burst):
			due = now.Sub(last) >= meshHomeRetry
		default:
			due = now.Sub(last) >= meshHomeProbeInterval
		}
		wasUp = up
		if !due {
			continue
		}
		last = now
		if !a.answers(ctx, a.config()) || ctx.Err() != nil {
			continue
		}
		if a.active.Load() > 0 {
			if !a.untilIdle(ctx) {
				return
			}
			if !a.answers(ctx, a.config()) || ctx.Err() != nil {
				continue
			}
		}
		a.moveTo(false, "the primary address answers through the mesh")
		a.finish(errReconnect)
		return
	}
}

// answers: a probe handshake with c.URL reaches the controller.
func (a *Agent) answers(ctx context.Context, c Config) bool {
	conn, _, first, err := a.dial(ctx, c, handshakeParams(true))
	if err != nil {
		return false
	}
	conn.Close()
	return first.T == "config"
}

type callKey struct{}

// callState follows one call from the controller.
type callState struct {
	// handedOver: the session it came on was ended to move off the mesh;
	// its reply goes out on the session that followed.
	handedOver atomic.Bool
}

// leaveMesh runs before an operation of this node that takes the mesh here
// down (what names it). If the session is on a mesh address and the other
// address answers, the session moves there first: leaveMesh returns once the
// new session said hello, or after handoverWait, with a context that the
// old session's end does not cancel. Until release, a session on the other
// address does not go back to the mesh.
func (a *Agent) leaveMesh(ctx context.Context, what string) (context.Context, func()) {
	a.mu.Lock()
	a.held++
	a.mu.Unlock()
	release := func() {
		a.mu.Lock()
		a.held--
		a.mu.Unlock()
	}
	onBackup := a.onBackup()
	if !a.meshExit(onBackup) {
		return ctx, release
	}
	probe := a.config()
	if !onBackup {
		probe.URL = probe.Backup
	}
	probeCtx, cancel := context.WithTimeout(ctx, 20*time.Second)
	ok := a.answers(probeCtx, probe)
	cancel()
	if !ok {
		a.Logger.Printf("%s takes the mesh down, but the other address does not answer; staying on the mesh address", what)
		return ctx, release
	}
	ready := make(chan struct{})
	a.mu.Lock()
	a.handover = ready
	a.mu.Unlock()
	a.moveTo(!onBackup, what+" takes the mesh down")
	if s, _ := ctx.Value(callKey{}).(*callState); s != nil {
		s.handedOver.Store(true)
	}
	a.finish(errReconnect)
	select {
	case <-ready:
	case <-time.After(handoverWait):
		a.Logger.Printf("no session on the other address yet; %s goes ahead", what)
	}
	return context.WithoutCancel(ctx), release
}

// sessionUp records the session whose hello just went out and ends a handover.
func (a *Agent) sessionUp(ch *channel) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.ch = ch
	if a.handover != nil {
		close(a.handover)
		a.handover = nil
	}
}

func (a *Agent) sessionDown(ch *channel) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.ch == ch {
		a.ch = nil
	}
}

// currentChannel is the channel of the session up now, if any.
func (a *Agent) currentChannel() *channel {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.ch
}
