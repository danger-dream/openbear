//go:build !unix && !windows

package node

import "context"

type terminals struct{}

func newTerminals() *terminals { return &terminals{} }

func (a *Agent) termOpen(_ context.Context, ch *channel, _ *terminals, m envelope) {
	ch.send(map[string]any{"t": "term.closed", "sid": m.Sid, "reason": "unsupported_platform"})
}

type terminal struct{}

func (t *terminal) stop(string)     {}
func (t *terminal) ack(int)         {}
func (t *terminal) resize(int, int) {}

func (ts *terminals) get(uint32) *terminal { return nil }
func (ts *terminals) input([]byte)         {}
func (ts *terminals) closeAll()            {}
