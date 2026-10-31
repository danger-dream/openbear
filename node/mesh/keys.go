package mesh

import (
	"crypto/ed25519"
	"errors"
	"os"
	"path/filepath"
	"strings"

	"openbear/node/mesh/sign"
)

// nodeKeys are this node's private keys besides wg.key and control.key,
// made by mesh.install or mesh.upgrade (node/mesh_node.go): sign.key signs
// what it says on its own behalf, e2e.key is the static key of its
// end-to-end sessions. Their public halves reach every other node in its
// config entry (Node.SignKey, Node.E2EKey).
type nodeKeys struct {
	sign ed25519.PrivateKey
	e2e  string // base64 X25519 private key
}

func loadKeys(dir string) (*nodeKeys, error) {
	s, err := sign.Load(filepath.Join(dir, "keys", "sign.key"))
	if err != nil {
		return nil, err
	}
	raw, err := os.ReadFile(filepath.Join(dir, "keys", "e2e.key"))
	if err != nil {
		return nil, err
	}
	e := strings.TrimSpace(string(raw))
	if _, err := keyHex(e); err != nil {
		return nil, errors.New("keys/e2e.key is not a base64 32-byte key")
	}
	return &nodeKeys{sign: s, e2e: e}, nil
}
