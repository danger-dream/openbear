package node

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/ecdh"
	"crypto/ed25519"
	"crypto/hkdf"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync"
)

// After the handshake every message is one binary WebSocket frame sealed with
// AES-256-GCM, so a TLS-terminating proxy or a plain-HTTP private link only
// carries ciphertext. Plaintext is [kind][payload].
const (
	kindJSON     = 0x00
	kindTerminal = 0x01
)

var errBadFrame = errors.New("undecryptable frame")

// handshakeBase binds both long-term signatures and the session keys to this
// node, the controller's fresh nonce and both ephemeral X25519 keys.
func handshakeBase(nodeID, nonce, controllerEph, nodeEph string) []byte {
	return []byte(nodeID + "\n" + nonce + "\n" + controllerEph + "\n" + nodeEph)
}

func signed(role string, base []byte) []byte {
	return append([]byte("openbear-node-v1/"+role+"\n"), base...)
}

// rejection is what the controller signs to refuse this node before there is
// a channel; base carries this node's fresh key share, or before the upgrade
// its nonce, so an old refusal cannot be replayed.
func rejection(base []byte, code int, reason string) []byte {
	return signed("reject", fmt.Appendf(base[:len(base):len(base)], "\n%d\n%s", code, reason))
}

// signedClose is the notice of a close whose reason ends in the controller's
// signature over this handshake; any other error comes back as it is.
func signedClose(err error, controller ed25519.PublicKey, base []byte) error {
	var closeErr *CloseError
	if !errors.As(err, &closeErr) {
		return err
	}
	i := strings.LastIndexByte(closeErr.Reason, ' ')
	if i < 0 {
		return err
	}
	reason := closeErr.Reason[:i]
	sig, decodeErr := base64.StdEncoding.DecodeString(closeErr.Reason[i+1:])
	if decodeErr != nil || !ed25519.Verify(controller, rejection(base, closeErr.Code, reason), sig) {
		return err
	}
	return notice(closeErr.Code, reason)
}

// upgradeNonce goes with the upgrade request, so the controller can sign a
// refusal it sends before the handshake.
func upgradeNonce() string {
	b := make([]byte, 16)
	rand.Read(b)
	return base64.StdEncoding.EncodeToString(b)
}

// signedUpgradeRefusal is the notice of a 401 the controller signed for nonce;
// any other error comes back as it is.
func signedUpgradeRefusal(err error, controller ed25519.PublicKey, nodeID, nonce string) error {
	var refused *HandshakeError
	if !errors.As(err, &refused) || refused.Status != http.StatusUnauthorized {
		return err
	}
	var body struct {
		Error string `json:"error"`
		Sig   string `json:"sig"`
	}
	if json.Unmarshal(refused.Body, &body) != nil {
		return err
	}
	sig, decodeErr := base64.StdEncoding.DecodeString(body.Sig)
	if decodeErr != nil || !ed25519.Verify(controller, rejection(handshakeBase(nodeID, nonce, "", ""), closeUnauthorized, body.Error), sig) {
		return err
	}
	return notice(closeUnauthorized, body.Error)
}

func sessionKeys(shared, base []byte) (nodeToController, controllerToNode cipher.AEAD, err error) {
	salt := sha256.Sum256(base)
	keys, err := hkdf.Key(sha256.New, shared, salt[:], "openbear-node-v1 keys", 64)
	if err != nil {
		return nil, nil, err
	}
	aead := func(key []byte) cipher.AEAD {
		block, _ := aes.NewCipher(key)
		g, _ := cipher.NewGCM(block)
		return g
	}
	return aead(keys[:32]), aead(keys[32:]), nil
}

type channel struct {
	conn *Conn
	tx   cipher.AEAD
	rx   cipher.AEAD
	mu   sync.Mutex // keeps the send counter in wire order
	sent uint64
	got  uint64
}

func counterNonce(n uint64) []byte {
	nonce := make([]byte, 12)
	binary.BigEndian.PutUint64(nonce[4:], n)
	return nonce
}

func (ch *channel) write(kind byte, parts ...[]byte) error {
	size := 1
	for _, p := range parts {
		size += len(p)
	}
	plain := make([]byte, 1, size)
	plain[0] = kind
	for _, p := range parts {
		plain = append(plain, p...)
	}
	ch.mu.Lock()
	defer ch.mu.Unlock()
	sealed := ch.tx.Seal(nil, counterNonce(ch.sent), plain, nil)
	ch.sent++
	return ch.conn.WriteBinary(sealed)
}

func (ch *channel) send(v any) error {
	data, err := json.Marshal(v)
	if err != nil {
		return err
	}
	return ch.write(kindJSON, data)
}

// read is only called from the session loop; frames are reliable and ordered,
// so any gap, replay or tampering fails authentication.
func (ch *channel) read() (byte, []byte, error) {
	op, data, err := ch.conn.Read()
	if err != nil {
		return 0, nil, err
	}
	if op != opBinary {
		return 0, nil, ch.conn.fail(1008, "plaintext after handshake")
	}
	plain, err := ch.rx.Open(data[:0], counterNonce(ch.got), data, nil)
	if err != nil || len(plain) == 0 {
		ch.conn.fail(1008, "bad frame")
		return 0, nil, errBadFrame
	}
	ch.got++
	return plain[0], plain[1:], nil
}

type handshake struct {
	Nonce string `json:"nonce"`
	Eph   string `json:"eph"`
	Sig   string `json:"sig"`
}

// secure runs the node side of the handshake on a fresh connection and
// returns the encrypted channel plus the controller's first message.
func secure(conn *Conn, nodeID string, key ed25519.PrivateKey, controller ed25519.PublicKey, hello map[string]any) (*channel, envelope, error) {
	var none envelope
	challenge, err := readPlain(conn)
	if err != nil {
		return nil, none, err
	}
	if challenge.T != "challenge" || challenge.Nonce == "" || challenge.Eph == "" {
		return nil, none, errors.New("controller sent no challenge")
	}
	peerRaw, err := base64.StdEncoding.DecodeString(challenge.Eph)
	if err != nil {
		return nil, none, errors.New("invalid controller key share")
	}
	peer, err := ecdh.X25519().NewPublicKey(peerRaw)
	if err != nil {
		return nil, none, errors.New("invalid controller key share")
	}
	eph, err := ecdh.X25519().GenerateKey(rand.Reader)
	if err != nil {
		return nil, none, err
	}
	mine := base64.StdEncoding.EncodeToString(eph.PublicKey().Bytes())
	base := handshakeBase(nodeID, challenge.Nonce, challenge.Eph, mine)
	// The parameters are signed too, so nothing on the way can change them;
	// the plain signature and copies are for controllers from before that.
	params, _ := json.Marshal(hello)
	bound := paramsBase(base, params)
	auth := map[string]any{"t": "auth", "node": nodeID, "eph": mine, "sig": base64.StdEncoding.EncodeToString(ed25519.Sign(key, signed("client", base))),
		"params": string(params), "paramsSig": base64.StdEncoding.EncodeToString(ed25519.Sign(key, signed("client-params", bound)))}
	for k, v := range hello {
		auth[k] = v
	}
	data, _ := json.Marshal(auth)
	if err = conn.WriteText(data); err != nil {
		return nil, none, err
	}
	welcome, err := readPlain(conn)
	if err != nil {
		return nil, none, signedClose(err, controller, base)
	}
	sig, _ := base64.StdEncoding.DecodeString(welcome.Sig)
	paramsSig, _ := base64.StdEncoding.DecodeString(welcome.ParamsSig)
	if welcome.T != "welcome" || !ed25519.Verify(controller, signed("server", base), sig) ||
		welcome.ParamsSig != "" && !ed25519.Verify(controller, signed("server-params", bound), paramsSig) {
		conn.CloseWith(closeUnauthorized, "controller identity mismatch")
		return nil, none, errControllerIdentity
	}
	shared, err := eph.ECDH(peer)
	if err != nil {
		return nil, none, err
	}
	tx, rx, err := sessionKeys(shared, base)
	if err != nil {
		return nil, none, err
	}
	ch := &channel{conn: conn, tx: tx, rx: rx}
	kind, payload, err := ch.read()
	if err != nil {
		return nil, none, err
	}
	var first envelope
	if kind != kindJSON || json.Unmarshal(payload, &first) != nil {
		return nil, none, errors.New("unexpected first message")
	}
	// A controller from before signed parameters says nothing about them.
	if first.SignedParams != nil && !*first.SignedParams {
		return nil, none, errParamsStripped
	}
	return ch, first, nil
}

// paramsBase is what the node signs for its handshake parameters (params, the
// JSON it sends), and a controller that took them then too.
func paramsBase(base, params []byte) []byte {
	return append(append(base[:len(base):len(base)], '\n'), params...)
}

var errParamsStripped = errors.New("the controller did not get this node's signed handshake parameters: something on the way removed them")

var errControllerIdentity = errors.New("controller identity does not match the enrolled key")

func readPlain(c *Conn) (envelope, error) {
	var e envelope
	op, data, err := c.Read()
	if err != nil {
		return e, err
	}
	if op != opText || json.Unmarshal(data, &e) != nil {
		return e, errors.New("unexpected message")
	}
	return e, nil
}
