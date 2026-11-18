package fast

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"net/netip"
	"sync"
	"time"

	"golang.org/x/crypto/chacha20poly1305"
	"golang.org/x/sys/cpu"
)

// The header every stream starts with on each hop, sealed with the mesh's
// fast key (AES-256-GCM when the CPU has AES instructions, ChaCha20-Poly1305
// otherwise): nonce (12) | sealed plain (112) | tag (16). Plain:
//
//	version | proto | flags | (nVisited | nHops<<4) | unix ms (8) |
//	dest (4) | port (2) | origin (4) | source (16) | source port (2) |
//	visited (8 x 4) | hops (8 x 4) | alt | 0 (7)
//
// dest, origin, visited and hops are mesh IPv4 addresses; source is the
// client's address as the destination's rules see it. Each hop strips itself
// from hops, adds itself to visited, and seals the header anew with a fresh
// random nonce, so every hop's wire header is indistinguishable from random
// bytes. A header not planned to its end is planned on by the first hop that
// can, over its alt-th path (0: its route); a probe goes like a stream and
// the destination answers it with the path it took (probeAnswer).
const (
	headerVersion = 2
	plainLen      = 112
	sealedLen     = 12 + plainLen + 16
	maxVisited    = 8
	maxHops       = 8
	flagPlanned   = 1
	flagProbe     = 2
	// headerWindow is how far a header's time may be from this clock's.
	headerWindow = 30 * time.Second
)

const (
	protoTCP = 1
	protoUDP = 2
)

type header struct {
	proto      byte
	planned    bool
	probe      bool
	alt        byte
	at         time.Time
	dest       netip.Addr
	port       uint16
	origin     netip.Addr
	source     netip.Addr
	sourcePort uint16
	visited    []netip.Addr
	hops       []netip.Addr
}

var (
	errSealed  = errors.New("header does not open")
	errFormat  = errors.New("header is malformed")
	errStale   = errors.New("header is too old or too new")
	errReplay  = errors.New("header seen before")
	errVisited = errors.New("header went through too many nodes")
)

func hasAESHardware() bool {
	return cpu.X86.HasAES || cpu.ARM64.HasAES || cpu.ARM.HasAES || cpu.S390X.HasAES
}

type keyPair struct {
	seal cipher.AEAD
	open [2]cipher.AEAD
}

type sealer struct {
	keys []keyPair // newest first
	seen *replays
}

func newSealer(keys []string) (*sealer, error) {
	return newSealerWith(keys, hasAESHardware())
}

func newSealerWith(keys []string, aesPreferred bool) (*sealer, error) {
	s := &sealer{seen: newReplays()}
	for _, k := range keys {
		b, err := base64.StdEncoding.DecodeString(k)
		if err != nil || len(b) != 32 {
			return nil, errors.New("fast key is not base64 of 32 bytes")
		}
		aesKey := deriveSubkey("openbear-fast-aes:", b)
		chaKey := deriveSubkey("openbear-fast-chacha:", b)
		block, err := aes.NewCipher(aesKey[:])
		if err != nil {
			return nil, err
		}
		gcm, err := cipher.NewGCM(block)
		if err != nil {
			return nil, err
		}
		cha, err := chacha20poly1305.New(chaKey[:])
		if err != nil {
			return nil, err
		}
		kp := keyPair{seal: gcm, open: [2]cipher.AEAD{gcm, cha}}
		if !aesPreferred {
			kp = keyPair{seal: cha, open: [2]cipher.AEAD{cha, gcm}}
		}
		s.keys = append(s.keys, kp)
	}
	return s, nil
}

func deriveSubkey(domain string, raw []byte) [32]byte {
	h := sha256.New()
	h.Write([]byte(domain))
	h.Write(raw)
	var out [32]byte
	copy(out[:], h.Sum(nil))
	return out
}

func (s *sealer) seal(h header) ([]byte, error) {
	if len(h.visited) > maxVisited || len(h.hops) > maxHops {
		return nil, errVisited
	}
	if !h.dest.Is4() || !h.origin.Is4() || h.port == 0 {
		return nil, errFormat
	}
	p := make([]byte, plainLen)
	var flags byte
	if h.planned {
		flags |= flagPlanned
	}
	if h.probe {
		flags |= flagProbe
	}
	p[0], p[1], p[2] = headerVersion, h.proto, flags
	p[104] = h.alt
	p[3] = byte(len(h.visited)) | byte(len(h.hops)<<4)
	binary.BigEndian.PutUint64(p[4:12], uint64(h.at.UnixMilli()))
	put4(p[12:16], h.dest)
	binary.BigEndian.PutUint16(p[16:18], h.port)
	put4(p[18:22], h.origin)
	src := h.source.As16()
	copy(p[22:38], src[:])
	binary.BigEndian.PutUint16(p[38:40], h.sourcePort)
	for i, v := range h.visited {
		if !v.Is4() {
			return nil, errFormat
		}
		put4(p[40+4*i:], v)
	}
	for i, v := range h.hops {
		if !v.Is4() {
			return nil, errFormat
		}
		put4(p[72+4*i:], v)
	}
	out := make([]byte, 12, sealedLen)
	if _, err := rand.Read(out); err != nil {
		return nil, err
	}
	return s.keys[0].seal.Seal(out, out[:12], p, nil), nil
}

// open checks b, sealedLen bytes, with any of the keys (AES-GCM or
// ChaCha20-Poly1305), its time against now and whether it came before.
func (s *sealer) open(b []byte, now time.Time) (header, error) {
	if len(b) != sealedLen {
		return header{}, errFormat
	}
	var p []byte
	for _, kp := range s.keys {
		for _, a := range kp.open {
			if out, err := a.Open(nil, b[:12], b[12:], nil); err == nil {
				p = out
				break
			}
		}
		if p != nil {
			break
		}
	}
	if p == nil {
		return header{}, errSealed
	}
	nv, nh := int(p[3]&0x0f), int(p[3]>>4)
	if p[0] != headerVersion || (p[1] != protoTCP && p[1] != protoUDP) || nv > maxVisited || nh > maxHops {
		return header{}, errFormat
	}
	port := binary.BigEndian.Uint16(p[16:18])
	if port == 0 {
		return header{}, errFormat
	}
	h := header{
		proto:      p[1],
		planned:    p[2]&flagPlanned != 0,
		probe:      p[2]&flagProbe != 0,
		alt:        p[104],
		at:         time.UnixMilli(int64(binary.BigEndian.Uint64(p[4:12]))),
		dest:       get4(p[12:16]),
		port:       port,
		origin:     get4(p[18:22]),
		source:     netip.AddrFrom16([16]byte(p[22:38])).Unmap(),
		sourcePort: binary.BigEndian.Uint16(p[38:40]),
	}
	for i := 0; i < nv; i++ {
		h.visited = append(h.visited, get4(p[40+4*i:]))
	}
	for i := 0; i < nh; i++ {
		h.hops = append(h.hops, get4(p[72+4*i:]))
	}
	if d := now.Sub(h.at); d > headerWindow || d < -headerWindow {
		return header{}, errStale
	}
	if !s.seen.add([12]byte(b[:12]), now) {
		return header{}, errReplay
	}
	return h, nil
}

func put4(b []byte, a netip.Addr) {
	v := a.As4()
	copy(b, v[:])
}

func get4(b []byte) netip.Addr { return netip.AddrFrom4([4]byte(b[:4])) }

// replays are the nonces of the headers opened within twice the window.
type replays struct {
	mu   sync.Mutex
	seen map[[12]byte]time.Time
	next time.Time
}

const maxReplays = 1 << 17

func newReplays() *replays { return &replays{seen: map[[12]byte]time.Time{}} }

func (r *replays) add(n [12]byte, now time.Time) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, dup := r.seen[n]; dup {
		return false
	}
	if now.After(r.next) || len(r.seen) >= maxReplays {
		for k, at := range r.seen {
			if now.Sub(at) > 2*headerWindow {
				delete(r.seen, k)
			}
		}
		r.next = now.Add(headerWindow)
	}
	if len(r.seen) >= maxReplays {
		return false
	}
	r.seen[n] = now
	return true
}
