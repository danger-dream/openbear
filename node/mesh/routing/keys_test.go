package routing

import (
	"crypto/ed25519"
	"crypto/sha256"
)

// testKey is the signing key tests give relay id.
func testKey(id string) ed25519.PrivateKey {
	seed := sha256.Sum256([]byte("openbear routing test key " + id))
	return ed25519.NewKeyFromSeed(seed[:])
}

// signed gives c the test keys of its relays, its mesh's id "n", and, unless
// it has some, every link between two of its relays as the mesh's.
func signed(c Config) Config {
	c.Network, c.Key = "n", testKey(c.Self)
	c.Keys = map[string]ed25519.PublicKey{c.Self: testKey(c.Self).Public().(ed25519.PublicKey)}
	all := append([]string{c.Self}, c.Relays...)
	for _, id := range c.Relays {
		c.Keys[id] = testKey(id).Public().(ed25519.PublicKey)
	}
	if c.RelayLinks == nil {
		for i, a := range all {
			for _, b := range all[i+1:] {
				if a != b {
					c.RelayLinks = append(c.RelayLinks, [2]string{a, b})
				}
			}
		}
	}
	return c
}

// signedBy is raw in format f as origin signs it.
func signedBy(origin string, f Format, raw []byte) Message {
	return Message{Format: f, Raw: raw, Sig: advertInput("n", f, raw).Sign(testKey(origin))}
}
