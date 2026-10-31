package health

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
)

const maxMessage = 1024

// message is one control message. Version is the mesh protocol (package
// wire) it is in; Min and Max are the range its sender speaks, so the
// receiver learns which version to answer in. Every message but a route
// advertisement carries them; from a peer whose range is not known, a
// message without them is from a node that speaks only its Version.
type message struct {
	Version    int     `json:"v"`
	Min        int     `json:"vmin,omitempty"`
	Max        int     `json:"vmax,omitempty"`
	Network    string  `json:"mesh"`
	Revision   int64   `json:"rev"`
	From       string  `json:"from"`
	To         string  `json:"to"`
	Type       string  `json:"type"`
	Nonce      string  `json:"nonce,omitempty"`
	Token      string  `json:"token,omitempty"`
	Generation uint64  `json:"gen,omitempty"`
	Probe      uint64  `json:"probe,omitempty"`
	Sequence   uint64  `json:"seq,omitempty"`
	Reclaim    bool    `json:"reclaim,omitempty"`
	Notice     *Notice `json:"notice,omitempty"`
	// MSig: a commit's claim, signed by the member (signing.go).
	MSig string `json:"msig,omitempty"`
	// Claim: a conflict's newer claim, as the member signed it.
	Claim *SignedClaim `json:"claim,omitempty"`
	// Direct is the sender's own readiness of a punched link.
	Direct bool        `json:"direct,omitempty"`
	Offer  *Offer      `json:"offer,omitempty"`
	Punch  *PunchState `json:"punch,omitempty"`
	Reach  *Reach      `json:"reach,omitempty"`
	// Advert is a relay's routing advertisement (routing.go).
	Advert json.RawMessage `json:"advert,omitempty"`
	// AdvertCompact is one in the compact form: Advert is empty then.
	AdvertCompact string `json:"ac,omitempty"`
	// AdvertSig is the origin's signature of the advertisement (package
	// routing), which relays forward with it unchanged.
	AdvertSig []byte `json:"as,omitempty"`
	// Trial marks a ping that measures a path a punch attempt opened.
	Trial bool `json:"trial,omitempty"`
	// Echo is what a pong asks the prober's "seen" to repeat, so that the
	// side that answered measures a round trip that ends no sooner than the
	// pong arrived. Nodes of the release before neither send nor repeat it.
	Echo string `json:"echo,omitempty"`
	// Observed is, in a pong, the address the answering node saw the ping
	// come from on this authenticated path; it checks what reflectors say
	// (entrycheck.go).
	Observed string `json:"obs,omitempty"`
}

// Offer carries a node's addresses to the other end of a punched link
// through relays. Session and Gen name what the sender last changed; Ack
// repeats the receiver's last such name the sender has seen. A Reply is not
// answered unless it brings something new or misses the receiver's own.
type Offer struct {
	From       string   `json:"from"`
	To         string   `json:"to"`
	Session    string   `json:"session"`
	Gen        uint64   `json:"gen"`
	Ack        string   `json:"ack,omitempty"`
	Reply      bool     `json:"reply,omitempty"`
	Candidates []string `json:"candidates,omitempty"`
	Hops       int      `json:"hops,omitempty"`
	// Sig is From's signature of all but Hops (signing.go).
	Sig string `json:"sig"`
}

// PunchState is one end's view of an attempt, sent through relays like an
// offer while the attempt lasts. Session and Attempt are the driving end's;
// Opened says the opener has primed its NAT.
type PunchState struct {
	From    string `json:"from"`
	To      string `json:"to"`
	Session string `json:"session"`
	Attempt uint64 `json:"attempt"`
	Opened  bool   `json:"opened,omitempty"`
	Hops    int    `json:"hops,omitempty"`
	// Probe: the attempt looks for a faster path beside a link that is up,
	// and does not take it down.
	Probe bool `json:"probe,omitempty"`
	// Sig is From's signature of all but Hops (signing.go).
	Sig string `json:"sig"`
}

// Reach asks a relay to test whether the address its reflector saw request
// ID come from takes packets from elsewhere ("reach"); the relay forwards
// the address it saw to other relays ("reach-relay") and answers
// ("reach-ack") with whether it saw the request and how many it asked.
type Reach struct {
	ID     string `json:"id"`
	Target string `json:"target,omitempty"`
	Seen   bool   `json:"seen,omitempty"`
	Others int    `json:"others,omitempty"`
}

func nonce() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic(err)
	}
	return hex.EncodeToString(b[:])
}
func decode(payload []byte) (message, error) {
	var m message
	if len(payload) > maxMessage {
		return m, errors.New("control payload exceeds limit")
	}
	// Any version decodes: the envelope is the same in all, and one this
	// node does not speak still says which ones its sender does.
	err := json.Unmarshal(payload, &m)
	if err == nil && (m.Version <= 0 || m.From == "" || m.To == "" || m.Network == "" || m.Revision <= 0) {
		err = errors.New("invalid control envelope")
	}
	return m, err
}
