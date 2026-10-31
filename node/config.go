package node

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"strings"
)

type Config struct {
	URL string `json:"url"`
	// Backup is used while URL keeps failing; both reach the same controller,
	// which the pinned ControllerKey proves on every connection.
	Backup     string `json:"backup,omitempty"`
	NodeID     string `json:"nodeId"`
	PrivateKey string `json:"privateKey"`
	// ControllerKey is pinned at enrollment; a controller that cannot sign
	// with it is refused even when TLS succeeds.
	ControllerKey string        `json:"controllerKey"`
	Capabilities  Capabilities  `json:"capabilities"`
	Browser       BrowserConfig `json:"browser,omitzero"`
}

// BrowserConfig chooses the browser behind Capabilities.Browser. With no
// Endpoint the node starts one itself: Executable, else an installed Chrome,
// Edge or Chromium, else a downloaded Chrome for Testing.
type BrowserConfig struct {
	// Endpoint is an already running browser's CDP address (ws:// or http://).
	Endpoint   string `json:"endpoint,omitempty"`
	Executable string `json:"executable,omitempty"`
	// Download uses Chrome for Testing even when a browser is installed.
	Download bool `json:"download,omitempty"`
}

func LoadConfig(path string) (Config, error) {
	var c Config
	f, err := os.Open(path)
	if err != nil {
		return c, err
	}
	defer f.Close()
	d := json.NewDecoder(io.LimitReader(f, 65537))
	d.DisallowUnknownFields()
	if err = d.Decode(&c); err != nil {
		return c, errors.New("invalid config JSON")
	}
	if _, err = ControllerURL(c.URL); err != nil {
		return c, err
	}
	if c.Backup != "" {
		if _, err = ControllerURL(c.Backup); err != nil {
			return c, err
		}
	}
	if c.NodeID == "" || len(c.NodeID) > 128 {
		return c, errors.New("config has no nodeId; run enroll first")
	}
	if _, err = c.Key(); err != nil {
		return c, err
	}
	if _, err = c.Controller(); err != nil {
		return c, err
	}
	return c, nil
}

func SaveConfig(path string, c Config) error {
	if err := secureDataDirs(filepath.Dir(path)); err != nil {
		return err
	}
	return writeConfig(path, c)
}

func writeConfig(path string, c Config) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	if err := protectDir(filepath.Dir(path)); err != nil {
		return err
	}
	data, _ := json.MarshalIndent(c, "", "  ")
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, append(data, '\n'), 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func (c Config) Key() (ed25519.PrivateKey, error) {
	seed, err := base64.StdEncoding.DecodeString(c.PrivateKey)
	if err != nil || len(seed) != ed25519.SeedSize {
		return nil, errors.New("config privateKey is invalid")
	}
	return ed25519.NewKeyFromSeed(seed), nil
}

func (c Config) Controller() (ed25519.PublicKey, error) {
	key, err := base64.StdEncoding.DecodeString(c.ControllerKey)
	if err != nil || len(key) != ed25519.PublicKeySize {
		return nil, errors.New("config controllerKey is invalid; re-enroll this node")
	}
	return ed25519.PublicKey(key), nil
}

// sharedAddressSpace is RFC 6598's 100.64.0.0/10, where meshes number their
// nodes: the controller's mesh address is one, reached over WireGuard.
var sharedAddressSpace = net.IPNet{IP: net.IPv4(100, 64, 0, 0), Mask: net.CIDRMask(10, 32)}

// ControllerURL accepts an https origin, or plain http only for loopback,
// private and shared (100.64.0.0/10) literal addresses (LAN, WireGuard, the
// mesh) where TLS is not available.
func ControllerURL(raw string) (*url.URL, error) {
	u, err := url.Parse(strings.TrimRight(strings.TrimSpace(raw), "/"))
	if err != nil || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return nil, errors.New("controller url must be an http(s) origin")
	}
	switch u.Scheme {
	case "https":
	case "http":
		host := u.Hostname()
		ip := net.ParseIP(host)
		if host != "localhost" && (ip == nil || !(ip.IsLoopback() || ip.IsPrivate() || sharedAddressSpace.Contains(ip))) {
			return nil, errors.New("plain http is only allowed for loopback, private or mesh addresses")
		}
	default:
		return nil, errors.New("controller url must be an http(s) origin")
	}
	return u, nil
}

func endpoint(base *url.URL, path string, websocket bool) string {
	u := *base
	u.Path = strings.TrimRight(u.Path, "/") + path
	if websocket {
		u.Scheme = map[string]string{"https": "wss", "http": "ws"}[u.Scheme]
	}
	return u.String()
}
