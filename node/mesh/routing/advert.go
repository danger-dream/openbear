package routing

import (
	"bytes"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"slices"
	"time"
)

// Advertisement parameters (README, "Advertisements").
const (
	advertEvery  = 5 * time.Second
	advertExpiry = 30 * time.Second
	// advertGap is the shortest time between two of this relay's own
	// advertisements, so a burst of changes goes out as one.
	advertGap = 100 * time.Millisecond
	// Each origin's new advertisements are accepted at originRate a second
	// with bursts of originBurst, twice what advertGap lets it send; each
	// neighbor may send senderRate a second in all, forwarded ones included.
	originRate, originBurst = 20.0, 40.0
	senderRate, senderBurst = 50.0, 100.0
	maxAdvertCost           = 1e6
)

// Format is how an advertisement is written on the wire.
type Format uint8

const (
	// FormatJSON names relays by id: {"o":origin,"s":seq,"l":[...],"r":[...]};
	// with long ids only a few relays fit. Used when some relay has no
	// distinct control host number.
	FormatJSON Format = 1
	// FormatCompact names relays by the host number of their control
	// address, in a binary form written as unpadded standard base64; it
	// holds at least 32 relays with their routes in one control message.
	FormatCompact Format = 2
)

// Budget is the largest advertisement one control message holds in each format.
type Budget struct{ JSON, Compact int }

// Message is one advertisement to send to a linked relay, with its
// origin's signature of Format and Raw.
type Message struct {
	Peer   string
	Format Format
	Raw    []byte
	Sig    []byte
}

// advert is one relay's link state: {origin, seq, links: [{peer, cost,
// state}]} and the next hop it uses for each destination. Routes is nil
// when the origin left them out to stay within the size limit.
type advert struct {
	origin string
	seq    uint64
	links  map[string]advLink
	routes map[string]string // destination -> next hop
	at     time.Time         // received
	// format, raw and sig: as the origin wrote and signed it, which is
	// what is forwarded.
	format Format
	raw    []byte
	sig    []byte
}

type advLink struct {
	cost  float64
	state string
}

// On the wire: {"o":origin,"s":seq,"l":[[peer,cost,"u"|"d"|"s"]...],"r":[[dest]|[dest,nextHop]...]}.
// A route through the destination itself is just [dest].
type wireAdvert struct {
	Origin string            `json:"o"`
	Seq    uint64            `json:"s"`
	Links  []json.RawMessage `json:"l"`
	Routes []json.RawMessage `json:"r,omitempty"`
}

var stateCode = map[string]string{StateUp: "u", StateDown: "d", StateSuppressed: "s"}
var codeState = map[string]string{"u": StateUp, "d": StateDown, "s": StateSuppressed}

// The compact form's state bytes.
var stateByte = map[string]byte{StateDown: 0, StateUp: 1, StateSuppressed: 2}
var byteState = map[byte]string{0: StateDown, 1: StateUp, 2: StateSuppressed}

const compactVersion = 2

func sortedLinks(a *advert) []string {
	out := make([]string, 0, len(a.links))
	for p := range a.links {
		out = append(out, p)
	}
	slices.Sort(out)
	return out
}

// encodeJSON writes a in FormatJSON; routes follow order.
func encodeJSON(a *advert, order []string, withRoutes bool) []byte {
	var b bytes.Buffer
	fmt.Fprintf(&b, `{"o":%s,"s":%d,"l":[`, quote(a.origin), a.seq)
	for i, p := range sortedLinks(a) {
		if i > 0 {
			b.WriteByte(',')
		}
		l := a.links[p]
		cost := l.cost
		if l.state != StateUp {
			cost = 0
		}
		fmt.Fprintf(&b, `[%s,%s,"%s"]`, quote(p), formatCost(cost), stateCode[l.state])
	}
	b.WriteByte(']')
	if withRoutes && a.routes != nil {
		b.WriteString(`,"r":[`)
		first := true
		for _, d := range order {
			nh, ok := a.routes[d]
			if !ok {
				continue
			}
			if !first {
				b.WriteByte(',')
			}
			first = false
			if nh == d {
				fmt.Fprintf(&b, `[%s]`, quote(d))
			} else {
				fmt.Fprintf(&b, `[%s,%s]`, quote(d), quote(nh))
			}
		}
		b.WriteByte(']')
	}
	b.WriteByte('}')
	return b.Bytes()
}

// encodeCompact writes a in FormatCompact: version 2, origin, seq, the
// links as (peer, state, cost in tenths of a ms when up), then a flag and
// the routes as (destination, next hop or 0 for the destination itself),
// every number an unsigned varint and every relay its control host
// number. ok is false when a relay has no number.
func encodeCompact(a *advert, num map[string]uint32, order []string, withRoutes bool) ([]byte, bool) {
	b := []byte{compactVersion}
	n, ok := num[a.origin]
	if !ok {
		return nil, false
	}
	b = binary.AppendUvarint(b, uint64(n))
	b = binary.AppendUvarint(b, a.seq)
	b = binary.AppendUvarint(b, uint64(len(a.links)))
	for _, p := range sortedLinks(a) {
		n, ok := num[p]
		if !ok {
			return nil, false
		}
		l := a.links[p]
		b = binary.AppendUvarint(b, uint64(n))
		b = append(b, stateByte[l.state])
		if l.state == StateUp {
			b = binary.AppendUvarint(b, uint64(math.Round(l.cost*10)))
		}
	}
	if !withRoutes || a.routes == nil {
		b = append(b, 0)
	} else {
		b = append(b, 1)
		var routes []uint64
		for _, d := range order {
			nh, has := a.routes[d]
			if !has {
				continue
			}
			dn, ok1 := num[d]
			hn, ok2 := num[nh]
			if !ok1 || !ok2 {
				return nil, false
			}
			if nh == d {
				hn = 0
			}
			routes = append(routes, uint64(dn), uint64(hn))
		}
		b = binary.AppendUvarint(b, uint64(len(routes)/2))
		for _, v := range routes {
			b = binary.AppendUvarint(b, v)
		}
	}
	out := make([]byte, base64.RawStdEncoding.EncodedLen(len(b)))
	base64.RawStdEncoding.Encode(out, b)
	return out, true
}

func quote(s string) string {
	raw, _ := json.Marshal(s)
	return string(raw)
}

func formatCost(c float64) string {
	raw, _ := json.Marshal(round1(c))
	return string(raw)
}

// builder collects a decoded advertisement and checks it against the
// relays this one knows: the same rules for both formats.
type builder struct {
	a     *advert
	known map[string]bool
}

func newBuilder(origin string, seq uint64, links, routes int, known map[string]bool) (*builder, error) {
	if !known[origin] || seq == 0 {
		return nil, errors.New("advert from an unknown relay")
	}
	if links > len(known) || routes > len(known) {
		return nil, errors.New("advert lists more relays than there are")
	}
	return &builder{a: &advert{origin: origin, seq: seq, links: map[string]advLink{}}, known: known}, nil
}

func (b *builder) link(peer string, cost float64, state string) error {
	if !b.known[peer] || peer == b.a.origin || state == "" || math.IsNaN(cost) || cost < 0 || cost > maxAdvertCost {
		return errors.New("advert link is invalid")
	}
	if _, dup := b.a.links[peer]; dup {
		return errors.New("advert repeats a link")
	}
	b.a.links[peer] = advLink{cost: round1(cost), state: state}
	return nil
}

func (b *builder) route(dest, nh string) error {
	if b.a.routes == nil {
		b.a.routes = map[string]string{}
	}
	if !b.known[dest] || !b.known[nh] || dest == b.a.origin || nh == b.a.origin {
		return errors.New("advert route is invalid")
	}
	if _, dup := b.a.routes[dest]; dup {
		return errors.New("advert repeats a route")
	}
	b.a.routes[dest] = nh
	return nil
}

// decodeJSON parses and checks a FormatJSON advertisement; it does not
// judge the sequence number.
func decodeJSON(raw []byte, known map[string]bool) (*advert, error) {
	var w wireAdvert
	if err := json.Unmarshal(raw, &w); err != nil {
		return nil, fmt.Errorf("advert: %w", err)
	}
	b, err := newBuilder(w.Origin, w.Seq, len(w.Links), len(w.Routes), known)
	if err != nil {
		return nil, err
	}
	for _, l := range w.Links {
		var f []any
		if err := json.Unmarshal(l, &f); err != nil || len(f) != 3 {
			return nil, errors.New("advert link is not [peer, cost, state]")
		}
		peer, _ := f[0].(string)
		cost, okCost := f[1].(float64)
		if !okCost {
			return nil, errors.New("advert link is invalid")
		}
		if err := b.link(peer, cost, codeState[fmt.Sprint(f[2])]); err != nil {
			return nil, err
		}
	}
	if w.Routes != nil {
		b.a.routes = map[string]string{}
		for _, r := range w.Routes {
			var f []string
			if err := json.Unmarshal(r, &f); err != nil || len(f) < 1 || len(f) > 2 {
				return nil, errors.New("advert route is not [dest] or [dest, nextHop]")
			}
			dest, nh := f[0], f[0]
			if len(f) == 2 {
				nh = f[1]
			}
			if err := b.route(dest, nh); err != nil {
				return nil, err
			}
		}
	}
	return b.a, nil
}

var errCompact = errors.New("compact advert is malformed")

// errNewerFormat: a compact advertisement of a later version, which a relay
// sends only once it knows the receiver reads it (package wire). It is
// ignored, not an error.
var errNewerFormat = errors.New("compact advert of a later version")

// decodeCompact parses and checks a FormatCompact advertisement; ids maps
// control host numbers to the relays this one knows.
func decodeCompact(raw []byte, ids map[uint32]string, known map[string]bool) (*advert, error) {
	data := make([]byte, base64.RawStdEncoding.DecodedLen(len(raw)))
	n, err := base64.RawStdEncoding.Decode(data, raw)
	if err == nil && n > 0 && data[0] > compactVersion {
		return nil, errNewerFormat
	}
	if err != nil || n == 0 || data[0] != compactVersion {
		return nil, errCompact
	}
	data = data[1:n]
	next := func() (uint64, bool) {
		v, k := binary.Uvarint(data)
		if k <= 0 {
			return 0, false
		}
		data = data[k:]
		return v, true
	}
	relay := func() (string, bool) {
		v, ok := next()
		if !ok || v > math.MaxUint32 {
			return "", false
		}
		id, ok := ids[uint32(v)]
		return id, ok
	}
	origin, ok1 := relay()
	seq, ok2 := next()
	links, ok3 := next()
	if !ok1 || !ok2 || !ok3 {
		return nil, errors.New("advert from an unknown relay")
	}
	b, err := newBuilder(origin, seq, int(min(links, uint64(len(known)+1))), 0, known)
	if err != nil {
		return nil, err
	}
	for range links {
		peer, ok := relay()
		if !ok || len(data) == 0 {
			return nil, errors.New("advert link is invalid")
		}
		state, ok := byteState[data[0]]
		data = data[1:]
		var cost uint64
		if ok && state == StateUp {
			cost, ok = next()
		}
		if !ok {
			return nil, errors.New("advert link is invalid")
		}
		if err := b.link(peer, float64(cost)/10, state); err != nil {
			return nil, err
		}
	}
	if len(data) == 0 {
		return nil, errCompact
	}
	has := data[0]
	data = data[1:]
	switch has {
	case 0:
	case 1:
		count, ok := next()
		if !ok || count > uint64(len(known)) {
			return nil, errors.New("advert lists more relays than there are")
		}
		b.a.routes = map[string]string{}
		for range count {
			dest, ok1 := relay()
			hv, ok2 := next()
			nh, found := dest, ok2
			if ok2 && hv != 0 {
				nh, found = ids[uint32(min(hv, math.MaxUint32))]
			}
			if !ok1 || !found {
				return nil, errors.New("advert route is invalid")
			}
			if err := b.route(dest, nh); err != nil {
				return nil, err
			}
		}
	default:
		return nil, errCompact
	}
	if len(data) != 0 {
		return nil, errCompact
	}
	return b.a, nil
}

// bucket is a token bucket.
type bucket struct {
	tokens float64
	at     time.Time
}

func (b *bucket) take(now time.Time, rate, burst float64) bool {
	if b.at.IsZero() {
		b.tokens = burst
	} else if now.After(b.at) {
		b.tokens = min(burst, b.tokens+now.Sub(b.at).Seconds()*rate)
	}
	b.at = now
	if b.tokens < 1 {
		return false
	}
	b.tokens--
	return true
}
