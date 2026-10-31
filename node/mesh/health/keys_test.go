package health

import (
	"crypto/ed25519"
	"crypto/sha256"
)

// signTestKey is the signing key tests give the node id: the same for every
// test, so one can sign as any node.
func signTestKey(id string) ed25519.PrivateKey {
	seed := sha256.Sum256([]byte("openbear mesh test key " + id))
	return ed25519.NewKeyFromSeed(seed[:])
}

// withKeys gives cfg's nodes and itself their test keys.
func withKeys(cfg Config) Config {
	nodes := make([]Node, len(cfg.Nodes))
	for i, n := range cfg.Nodes {
		n.SignKey = signTestKey(n.ID).Public().(ed25519.PublicKey)
		nodes[i] = n
	}
	cfg.Nodes = nodes
	cfg.SignKey = signTestKey(cfg.Self)
	return cfg
}

// keysOf is the manager's view of the keys of cfg, for managers tests
// build field by field.
func keysOf(cfg Config) map[string]ed25519.PublicKey { return signKeys(withKeys(cfg)) }

// sn is n signed by its member and owner with their test keys, in mesh "n".
func sn(n Notice) Notice {
	n.MSig = claimInput("n", n.Member, n.Owner, n.Seq).SignString(signTestKey(n.Member))
	n.OSig = noticeInput("n", n).SignString(signTestKey(n.Owner))
	return n
}

// configureOwners configures o in mesh "n" with the test keys of every node
// allowed and relays name.
func configureOwners(o *owners, revision int64, allowed map[string][]string, relays map[string]bool) error {
	keys := map[string]ed25519.PublicKey{}
	add := func(id string) { keys[id] = signTestKey(id).Public().(ed25519.PublicKey) }
	for m, rs := range allowed {
		add(m)
		for _, r := range rs {
			add(r)
		}
	}
	for r := range relays {
		add(r)
	}
	_, err := o.configure(revision, allowed, relays, "n", keys)
	return err
}
