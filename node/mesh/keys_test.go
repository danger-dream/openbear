package mesh

import (
	"crypto/ecdh"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"os"
	"path/filepath"
	"testing"

	"openbear/node/mesh/sign"
)

// Tests give every node id the same signing and end-to-end keys, so a
// config can name a node's keys before the node's directory exists.

func testSignKey(id string) ed25519.PrivateKey {
	seed := sha256.Sum256([]byte("openbear mesh test sign key " + id))
	return ed25519.NewKeyFromSeed(seed[:])
}

func testE2EKey(id string) *ecdh.PrivateKey {
	seed := sha256.Sum256([]byte("openbear mesh test e2e key " + id))
	k, err := ecdh.X25519().NewPrivateKey(seed[:])
	if err != nil {
		panic(err)
	}
	return k
}

// testSignPub and testE2EPub are what the controller puts in id's entry.
func testSignPub(id string) string { return sign.Public(testSignKey(id)) }
func testE2EPub(id string) string {
	return base64.StdEncoding.EncodeToString(testE2EKey(id).PublicKey().Bytes())
}

// writeTestKeys writes id's keys/sign.key and keys/e2e.key into dir, as
// mesh.upgrade does.
func writeTestKeys(t testing.TB, dir, id string) {
	t.Helper()
	keys := filepath.Join(dir, "keys")
	if err := os.MkdirAll(keys, 0o700); err != nil {
		t.Fatal(err)
	}
	seed := testSignKey(id).Seed()
	if err := os.WriteFile(filepath.Join(keys, "sign.key"), []byte(base64.StdEncoding.EncodeToString(seed)+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(keys, "e2e.key"), []byte(base64.StdEncoding.EncodeToString(testE2EKey(id).Bytes())+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
}

// withTestKeys gives every node of c its test keys.
func withTestKeys(c *Config) *Config {
	for i := range c.Nodes {
		c.Nodes[i].SignKey, c.Nodes[i].E2EKey = testSignPub(c.Nodes[i].ID), testE2EPub(c.Nodes[i].ID)
	}
	return c
}

// relayLinksOf is every pair of relays of c, this one included, each once:
// a full mesh of relays, as the lab configures one.
func relayLinksOf(c *Config) [][2]string {
	relays := []string{}
	if c.isRelay() {
		relays = append(relays, c.Self.ID)
	}
	for _, n := range c.Nodes {
		if n.Role == roleRelay {
			relays = append(relays, n.ID)
		}
	}
	var out [][2]string
	for i, a := range relays {
		for _, b := range relays[i+1:] {
			out = append(out, [2]string{a, b})
		}
	}
	return out
}
