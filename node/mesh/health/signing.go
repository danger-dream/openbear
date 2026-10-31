package health

import (
	"crypto/ed25519"
	"maps"
	"strconv"
	"time"

	"openbear/node/mesh/sign"
)

// limitedEvery is how often one kind of security event is emitted at most.
const limitedEvery = time.Minute

type limitedEvent struct {
	at      time.Time
	skipped int
}

// What a node says on another's behalf is signed by the node it speaks
// for (package sign): a member's claim to attach to a relay at a sequence,
// the owner relay's lease of that claim, and the offers and punch states
// of punched links. Relays forward them unchanged; each node checks the
// signature of the node a message is in the name of with that node's key
// from the config. Nothing unsigned is taken: the whole mesh runs this
// protocol version.

// claimInput: member claims to attach to owner at seq.
func claimInput(network, member, owner string, seq uint64) *sign.Input {
	return sign.New(sign.Claim, network).String(member).String(owner).Uint(seq)
}

// noticeInput: the owner leases n, the member's claim, whose signature it
// covers too.
func noticeInput(network string, n Notice) *sign.Input {
	return sign.New(sign.Notice, network).String(n.Member).String(n.Owner).Uint(n.Seq).Uint(n.Refresh).
		Int(n.Generation).Int(n.LeaseMs).String(n.MSig)
}

// offerInput covers everything of an offer but the hop count relays raise.
func offerInput(network string, o *Offer) *sign.Input {
	return sign.New(sign.Offer, network).String(o.From).String(o.To).String(o.Session).Uint(o.Gen).
		String(o.Ack).Bool(o.Reply).Strings(o.Candidates)
}

func punchInput(network string, ps *PunchState) *sign.Input {
	return sign.New(sign.Punch, network).String(ps.From).String(ps.To).String(ps.Session).Uint(ps.Attempt).
		Bool(ps.Opened).Bool(ps.Probe)
}

// signKeys maps each node of c, this one included, to its signing key.
func signKeys(c Config) map[string]ed25519.PublicKey {
	out := map[string]ed25519.PublicKey{}
	for _, n := range c.Nodes {
		if len(n.SignKey) == ed25519.PublicKeySize {
			out[n.ID] = n.SignKey
		}
	}
	if len(c.SignKey) == ed25519.PrivateKeySize {
		out[c.Self] = c.SignKey.Public().(ed25519.PublicKey)
	}
	return out
}

// signedNotice is whether both signatures of n hold under keys.
func signedNotice(network string, n Notice, keys map[string]ed25519.PublicKey) bool {
	return claimInput(network, n.Member, n.Owner, n.Seq).VerifyString(keys[n.Member], n.MSig) &&
		noticeInput(network, n).VerifyString(keys[n.Owner], n.OSig)
}

// verified counts a check of a signature on what came from hop in the name
// of from, and says what it found.
func (m *Manager) verified(ok bool, what, from, hop string) bool {
	if ok {
		m.signVerified++
		return true
	}
	m.signRejected++
	if m.rejectedBy == nil {
		m.rejectedBy = map[string]uint64{}
	}
	m.rejectedBy[hop]++
	m.emitLimited("sign.rejected", what+" in the name of "+from+" from "+hop+": bad signature")
	return false
}

// securityStatus counts the signatures this node checked, its router's
// and its notices' included, and the stored claims it dropped.
func (m *Manager) securityStatus() SecurityStatus {
	s := SecurityStatus{Verified: m.signVerified + m.owners.verified, Rejected: m.signRejected, ClaimsExpired: m.claimsExpired}
	if m.relay.router != nil {
		s.Verified += m.relay.router.Verified()
	}
	if len(m.rejectedBy) > 0 {
		s.RejectedBy = maps.Clone(m.rejectedBy)
	}
	return s
}

// emitLimited emits an event of kind at most once a minute, with how many
// were left out in between.
func (m *Manager) emitLimited(kind, detail string) {
	now := time.Now()
	if m.limited == nil {
		m.limited = map[string]limitedEvent{}
	}
	l := m.limited[kind]
	if !l.at.IsZero() && now.Sub(l.at) < limitedEvery {
		l.skipped++
		m.limited[kind] = l
		return
	}
	if l.skipped > 0 {
		detail += " (" + strconv.Itoa(l.skipped) + " more since the last one)"
	}
	m.limited[kind] = limitedEvent{at: now}
	m.emit(kind, detail)
}
