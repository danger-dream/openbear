// Package sign is how mesh nodes sign what they say on another node's
// behalf: an Ed25519 key per node (keys/sign.key), whose public half the
// controller hands every other node, and a fixed binary encoding of each
// signed message so that the same fields always give the same bytes.
//
// Every signed input starts with "OBMSIG1", the kind of message and the
// mesh's id, so a signature made for one kind or one mesh is no use for
// another. Fields follow in a fixed order: strings and byte strings with a
// two-byte big-endian length, integers as eight bytes big-endian, booleans
// as one byte, lists with a two-byte count.
package sign

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"os"
	"path/filepath"
	"strings"
)

// Kind is what a signature is for.
type Kind byte

const (
	// Claim: a member says which relay it attaches to, at which sequence.
	Claim Kind = 1
	// Notice: the owner relay's lease of a member's claim.
	Notice Kind = 2
	// Advert: a relay's routing advertisement.
	Advert Kind = 3
	// Offer: a node's addresses for a punched link.
	Offer Kind = 4
	// Punch: one end's state of a punch attempt.
	Punch Kind = 5
)

// Size is the length of a signature.
const Size = ed25519.SignatureSize

const magic = "OBMSIG1"

// Input builds the bytes a signature covers.
type Input struct{ b []byte }

// New starts the input of a kind of message in a mesh.
func New(k Kind, network string) *Input {
	in := &Input{b: append([]byte(magic), byte(k))}
	return in.String(network)
}

func (in *Input) String(s string) *Input {
	if len(s) > 0xffff {
		s = s[:0xffff]
	}
	in.b = binary.BigEndian.AppendUint16(in.b, uint16(len(s)))
	in.b = append(in.b, s...)
	return in
}

func (in *Input) Bytes(p []byte) *Input {
	if len(p) > 0xffff {
		p = p[:0xffff]
	}
	in.b = binary.BigEndian.AppendUint16(in.b, uint16(len(p)))
	in.b = append(in.b, p...)
	return in
}

func (in *Input) Uint(v uint64) *Input {
	in.b = binary.BigEndian.AppendUint64(in.b, v)
	return in
}

func (in *Input) Int(v int64) *Input { return in.Uint(uint64(v)) }

func (in *Input) Bool(v bool) *Input {
	if v {
		in.b = append(in.b, 1)
	} else {
		in.b = append(in.b, 0)
	}
	return in
}

func (in *Input) Strings(list []string) *Input {
	n := min(len(list), 0xffff)
	in.b = binary.BigEndian.AppendUint16(in.b, uint16(n))
	for _, s := range list[:n] {
		in.String(s)
	}
	return in
}

// Sum is the input so far.
func (in *Input) Sum() []byte { return in.b }

// Sign signs the input with key.
func (in *Input) Sign(key ed25519.PrivateKey) []byte { return ed25519.Sign(key, in.b) }

// SignString is Sign as standard base64, the form JSON messages carry.
func (in *Input) SignString(key ed25519.PrivateKey) string {
	return base64.StdEncoding.EncodeToString(in.Sign(key))
}

// Verify says whether sig is pub's signature of the input. A nil key
// verifies nothing.
func (in *Input) Verify(pub ed25519.PublicKey, sig []byte) bool {
	return len(pub) == ed25519.PublicKeySize && len(sig) == Size && ed25519.Verify(pub, in.b, sig)
}

// VerifyString is Verify of a base64 signature.
func (in *Input) VerifyString(pub ed25519.PublicKey, sig string) bool {
	if len(sig) != base64.StdEncoding.EncodedLen(Size) {
		return false
	}
	raw, err := base64.StdEncoding.DecodeString(sig)
	return err == nil && in.Verify(pub, raw)
}

// ParsePublic decodes a base64 public key.
func ParsePublic(s string) (ed25519.PublicKey, error) {
	raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(s))
	if err != nil || len(raw) != ed25519.PublicKeySize {
		return nil, errors.New("not a base64 Ed25519 public key")
	}
	return ed25519.PublicKey(raw), nil
}

// Public is the base64 public half of key.
func Public(key ed25519.PrivateKey) string {
	return base64.StdEncoding.EncodeToString(key.Public().(ed25519.PublicKey))
}

// ParsePrivate decodes a key file's content: the 32-byte seed, base64.
func ParsePrivate(content []byte) (ed25519.PrivateKey, error) {
	seed, err := base64.StdEncoding.DecodeString(string(bytes.TrimSpace(content)))
	if err != nil || len(seed) != ed25519.SeedSize {
		return nil, errors.New("not a base64 Ed25519 seed")
	}
	return ed25519.NewKeyFromSeed(seed), nil
}

// Load reads the key in path.
func Load(path string) (ed25519.PrivateKey, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	key, err := ParsePrivate(raw)
	if err != nil {
		return nil, errors.New(filepath.Base(path) + ": " + err.Error())
	}
	return key, nil
}

// Ensure returns the key in path, making one (mode 0600) when there is none.
func Ensure(path string) (ed25519.PrivateKey, error) {
	key, err := Load(path)
	if err == nil || !errors.Is(err, os.ErrNotExist) {
		return key, err
	}
	seed := make([]byte, ed25519.SeedSize)
	if _, err := rand.Read(seed); err != nil {
		return nil, err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, []byte(base64.StdEncoding.EncodeToString(seed)+"\n"), 0o600); err != nil {
		return nil, err
	}
	if err := os.Rename(tmp, path); err != nil {
		os.Remove(tmp)
		return nil, err
	}
	return ed25519.NewKeyFromSeed(seed), nil
}
