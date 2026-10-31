package health

import (
	"crypto/ed25519"
	"errors"
	"slices"
	"time"
)

var (
	errUnauthorized = errors.New("unauthorized ownership notice")
	errReplay       = errors.New("stale ownership notice")
	errConflict     = errors.New("conflicting attachment sequence")
	errSignature    = errors.New("ownership notice not signed by its member and owner")
)

type Notice struct {
	Member     string `json:"member"`
	Owner      string `json:"owner"`
	Seq        uint64 `json:"seq"`
	Refresh    uint64 `json:"refresh"`
	Generation int64  `json:"generation"`
	LeaseMs    int64  `json:"leaseMs"`
	// MSig is the member's signature of its claim (Member, Owner, Seq);
	// OSig the owner's of all the rest and MSig (signing.go).
	MSig string `json:"msig"`
	OSig string `json:"osig"`
}

// base is n without its signatures: what an acknowledgement repeats.
func (n Notice) base() Notice {
	n.MSig, n.OSig = "", ""
	return n
}

// SignedClaim is a member's signed claim as a relay holds it, sent back in
// a conflict so the member sees it signed the sequence itself.
type SignedClaim struct {
	Owner string `json:"owner"`
	Seq   uint64 `json:"seq"`
	MSig  string `json:"msig"`
}

type OwnerStatus struct {
	Relay    string
	Seq      uint64
	LeaseMs  int64
	Conflict bool
}

type owners struct {
	store   *store
	relays  map[string]bool
	expires map[string]time.Time
	lease   time.Duration
	// network and keys check the signatures of notices (signing.go).
	network string
	keys    map[string]ed25519.PublicKey
	// verified counts the notices whose signatures held.
	verified uint64
}

func newOwners(s *store, lease time.Duration) *owners {
	return &owners{store: s, lease: lease, relays: map[string]bool{}, expires: map[string]time.Time{}}
}

// configure takes the members' allowed relays, the relays, and the signing
// keys of every node. A stored claim whose signatures do not hold under
// those keys is dropped; dropped counts them.
func (o *owners) configure(revision int64, allowed map[string][]string, relays map[string]bool, network string, keys map[string]ed25519.PublicKey) (dropped int, err error) {
	p := o.store.copy()
	if revision < p.Revision {
		return 0, errors.New("attachment config revision regressed")
	}
	for id, c := range p.Claims {
		n := Notice{Member: id, Owner: c.Owner, Seq: c.Seq, Refresh: c.Refresh, Generation: c.Generation, LeaseMs: c.LeaseMs, MSig: c.MSig, OSig: c.OSig}
		if signedNotice(network, n, keys) {
			continue
		}
		delete(p.Claims, id)
		delete(o.expires, id)
		dropped++
	}
	changed := false
	for id, old := range p.Authorized {
		if !slices.Equal(old, allowed[id]) {
			changed = true
			if revision <= p.Revision {
				return 0, errors.New("authorization change requires a newer revision")
			}
			p.Revoked[id] = revision
			delete(p.Claims, id)
		}
	}
	if revision == p.Revision {
		for id, current := range allowed {
			if !slices.Equal(current, p.Authorized[id]) {
				return 0, errors.New("authorization change requires a newer revision")
			}
		}
	}
	p.Revision = revision
	p.Authorized = map[string][]string{}
	for id, a := range allowed {
		p.Authorized[id] = append([]string(nil), a...)
	}
	if err := o.store.commit(p); err != nil {
		return 0, err
	}
	o.relays, o.network, o.keys = relays, network, keys
	if changed {
		for id := range o.expires {
			if _, ok := p.Claims[id]; !ok {
				delete(o.expires, id)
			}
		}
	}
	return dropped, nil
}

func (o *owners) accept(from string, n Notice, now time.Time) (bool, error) {
	p := o.store.copy()
	if !o.relays[from] || !o.relays[n.Owner] || n.Generation < p.Revoked[n.Member] || !slices.Contains(p.Authorized[n.Member], n.Owner) || n.Seq == 0 || n.Refresh == 0 || n.LeaseMs <= 0 || time.Duration(n.LeaseMs)*time.Millisecond > o.lease {
		return false, errUnauthorized
	}
	old, exists := p.Claims[n.Member]
	if exists && n.Seq < old.Seq {
		return false, errReplay
	}
	if exists && n.Seq == old.Seq && n.Owner == old.Owner && !old.Conflict && n.Refresh <= old.Refresh {
		return false, errReplay
	}
	// Checked after the cheap replay tests: an old notice costs no
	// verification, and only a signed one may mark a conflict.
	if !signedNotice(o.network, n, o.keys) {
		return false, errSignature
	}
	o.verified++
	if exists && n.Seq == old.Seq {
		if n.Owner != old.Owner {
			old.Conflict = true
			p.Claims[n.Member] = old
			o.store.stage(p)
			return false, errConflict
		}
		if old.Conflict {
			return false, errConflict
		}
		if n.Refresh <= old.Refresh {
			return false, errReplay
		}
	}
	p.Claims[n.Member] = claim{Owner: n.Owner, Seq: n.Seq, Refresh: n.Refresh, Generation: n.Generation, LeaseMs: n.LeaseMs, MSig: n.MSig, OSig: n.OSig}
	o.store.stage(p)
	changed := !exists || old.Owner != n.Owner || !now.Before(o.expires[n.Member])
	o.expires[n.Member] = now.Add(time.Duration(n.LeaseMs) * time.Millisecond)
	return changed, nil
}

func (o *owners) owner(member string, now time.Time) (string, bool) {
	c, ok := o.store.state.Claims[member]
	return c.Owner, ok && now.Before(o.expires[member]) && slices.Contains(o.store.state.Authorized[member], c.Owner)
}

func (o *owners) snapshot(now time.Time) map[string]OwnerStatus {
	out := map[string]OwnerStatus{}
	for id, c := range o.store.state.Claims {
		if _, ok := o.owner(id, now); ok {
			out[id] = OwnerStatus{c.Owner, c.Seq, max(0, o.expires[id].Sub(now).Milliseconds()), c.Conflict}
		}
	}
	return out
}
