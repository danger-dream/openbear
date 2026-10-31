package mesh

import "sync/atomic"

// Attachment says where business traffic goes. The static implementation answers from the config;
// the health package replaces it with probing, failover and relay-to-relay ownership notices.
type Attachment interface {
	Owner(member string) (relay string, ok bool) // relay role
	ActiveEntry() (candidateID string, ok bool)  // member role
	Changes() <-chan struct{}                    // signalled after any change
}

// configured is implemented by attachments that follow config updates.
// The data plane calls it before computing peers for a new config, and
// again with the old config when applying the new one fails.
type configured interface {
	SetConfig(c *Config)
}

// directRoutes is implemented by attachments that punch direct links.
type directRoutes interface {
	Direct(peer string) bool
}

// staticAttachment attaches every member to the first relay it lists and
// uses the first entry candidate.
type staticAttachment struct {
	cfg     atomic.Pointer[Config]
	changes chan struct{}
}

func newStaticAttachment(c *Config) *staticAttachment {
	a := &staticAttachment{changes: make(chan struct{}, 1)}
	a.cfg.Store(c)
	return a
}

func (a *staticAttachment) SetConfig(c *Config) { a.cfg.Store(c) }

func (a *staticAttachment) Owner(member string) (string, bool) {
	n := a.cfg.Load().node(member)
	if n == nil || len(n.Relays) == 0 {
		return "", false
	}
	return n.Relays[0], true
}

func (a *staticAttachment) ActiveEntry() (string, bool) {
	e := a.cfg.Load().Entry
	if e == nil || len(e.Candidates) == 0 {
		return "", false
	}
	return e.Candidates[0].ID, true
}

// Changes never fires: the static answers change only with the config,
// which the data plane applies itself.
func (a *staticAttachment) Changes() <-chan struct{} { return a.changes }

// NewAttachment builds the attachment for a data plane starting with c.
var NewAttachment = func(c *Config) Attachment { return newHealthAttachment(c) }
