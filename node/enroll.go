package node

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// Enroll registers a freshly generated key with a one-time code. Only the
// public key leaves this machine. fingerprint, when the install command gives
// one, is the SHA-256 (hex) of the controller's identity key: a response with
// another key, as from anything on a plain-http path, ends the enrollment.
func Enroll(ctx context.Context, rawURL, backup, code, fingerprint string, caps Capabilities) (Config, error) {
	base, err := ControllerURL(rawURL)
	if err != nil {
		return Config{}, err
	}
	fingerprint = strings.ToLower(strings.TrimSpace(fingerprint))
	if fingerprint != "" {
		if raw, err := hex.DecodeString(fingerprint); err != nil || len(raw) != sha256.Size {
			return Config{}, errors.New("the controller fingerprint must be 64 hex digits")
		}
	}
	var spare *url.URL
	if backup != "" {
		if spare, err = ControllerURL(backup); err != nil {
			return Config{}, err
		}
	}
	if code == "" || len(code) > 128 {
		return Config{}, errors.New("enrollment code is required")
	}
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return Config{}, err
	}
	info := NewCollector().Info()
	body, _ := json.Marshal(map[string]any{"code": code, "publicKey": base64.StdEncoding.EncodeToString(pub),
		"info": info, "version": Version, "protocol": ProtocolVersion, "protocolMin": ProtocolMin})
	ctx, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()
	client := &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	post := func(target *url.URL) (*http.Response, error) {
		req, _ := http.NewRequestWithContext(ctx, http.MethodPost, endpoint(target, "/api/server-nodes/v1/enroll", false), bytes.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("User-Agent", "openbear-node/"+Version)
		return client.Do(req)
	}
	// A code is spent only by a response, so retrying through the backup after
	// a network failure cannot enroll twice.
	resp, err := post(base)
	if err != nil && spare != nil {
		resp, err = post(spare)
	}
	if err != nil {
		return Config{}, err
	}
	defer resp.Body.Close()
	var result struct {
		OK            bool   `json:"ok"`
		NodeID        string `json:"nodeId"`
		ControllerKey string `json:"controllerKey"`
		Error         string `json:"error"`
	}
	if err = json.NewDecoder(io.LimitReader(resp.Body, 65536)).Decode(&result); err != nil {
		return Config{}, fmt.Errorf("enrollment failed: HTTP %d", resp.StatusCode)
	}
	if !result.OK || result.NodeID == "" {
		if strings.HasSuffix(result.Error, "_protocol_outdated") {
			return Config{}, fmt.Errorf("enrollment failed: %s (this node speaks protocol %d..%d, the controller none of them)", result.Error, ProtocolMin, ProtocolVersion)
		}
		return Config{}, fmt.Errorf("enrollment failed: %s", result.Error)
	}
	c := Config{URL: base.String(), NodeID: result.NodeID, PrivateKey: base64.StdEncoding.EncodeToString(priv.Seed()),
		ControllerKey: result.ControllerKey, Capabilities: caps}
	if spare != nil {
		c.Backup = spare.String()
	}
	controller, err := c.Controller()
	if err != nil {
		return Config{}, errors.New("enrollment failed: controller sent no identity key")
	}
	if sum := sha256.Sum256(controller); fingerprint != "" && hex.EncodeToString(sum[:]) != fingerprint {
		return Config{}, fmt.Errorf("enrollment stopped: the controller answered with an identity key whose fingerprint is %x, not %s "+
			"as in the install command; something on the way may be posing as it. Nothing was saved", sum, fingerprint)
	}
	return c, nil
}
