package gateway

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

type Config struct {
	Listen        string          `json:"listen"`
	ControlListen string          `json:"controlListen,omitempty"` // control API only, normally loopback; unset: shares listen
	PublicURL     string          `json:"publicUrl"`
	StateDir      string          `json:"stateDir"`
	WebRoot       string          `json:"webRoot"`
	ControlKey    string          `json:"controlKey"`
	TLSCert       string          `json:"tlsCert"`
	TLSKey        string          `json:"tlsKey"`
	ClientIP      *ClientIPConfig `json:"clientIp,omitempty"`
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
	if err = d.Decode(new(any)); err != io.EOF {
		return c, errors.New("invalid config trailing data")
	}
	return c, c.validate()
}
func validSecret(s string) bool {
	return len(s) >= 32 && len(s) <= 4096 && !strings.ContainsAny(s, "\r\n\x00")
}
func validID(s string) bool {
	if len(s) < 1 || len(s) > 128 {
		return false
	}
	for _, c := range s {
		if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '_' || c == '-') {
			return false
		}
	}
	return true
}
func parseURL(s string, root bool) (*url.URL, error) {
	u, err := url.Parse(s)
	if err != nil || u == nil {
		return nil, errors.New("invalid HTTP URL")
	}
	if (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" || u.User != nil || u.RawQuery != "" || u.ForceQuery || u.Fragment != "" || u.Opaque != "" || strings.ContainsAny(u.Host, "\\% \t\r\n") {
		return nil, errors.New("invalid HTTP URL")
	}
	if root && u.Path != "" && u.Path != "/" {
		return nil, errors.New("URL must be a root origin")
	}
	if u.RawPath != "" || strings.Contains(u.Path, "\\") || strings.Contains(u.Path, "//") {
		return nil, errors.New("invalid URL path")
	}
	for _, p := range strings.Split(u.Path, "/") {
		if p == "." || p == ".." {
			return nil, errors.New("invalid URL path")
		}
	}
	h := u.Hostname()
	if h == "" {
		return nil, errors.New("invalid host")
	}
	if net.ParseIP(h) == nil {
		if strings.Contains(h, ":") || strings.ContainsAny(u.Host, "[]") || len(h) > 253 {
			return nil, errors.New("invalid host")
		}
		for _, part := range strings.Split(strings.TrimSuffix(h, "."), ".") {
			if len(part) == 0 || len(part) > 63 || part[0] == '-' || part[len(part)-1] == '-' {
				return nil, errors.New("invalid host")
			}
			for _, r := range part {
				if !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '-') {
					return nil, errors.New("invalid host")
				}
			}
		}
	}
	if strings.HasSuffix(u.Host, ":") {
		return nil, errors.New("invalid port")
	}
	if p := u.Port(); p != "" {
		n, e := strconv.Atoi(p)
		if e != nil || n < 1 || n > 65535 {
			return nil, errors.New("invalid port")
		}
	}
	u.Scheme = strings.ToLower(u.Scheme)
	u.Host = strings.ToLower(u.Host)
	if root {
		u.Path = ""
	}
	return u, nil
}
func origin(u *url.URL) string {
	port := u.Port()
	if port == "" {
		if u.Scheme == "https" {
			port = "443"
		} else {
			port = "80"
		}
	}
	return u.Scheme + "://" + net.JoinHostPort(strings.TrimSuffix(strings.ToLower(u.Hostname()), "."), port)
}

func (c *Config) validate() error {
	if c.Listen == "" {
		c.Listen = "0.0.0.0:18962"
	}
	if _, _, e := net.SplitHostPort(c.Listen); e != nil {
		return errors.New("listen must be host:port")
	}
	if c.ControlListen != "" {
		if _, _, e := net.SplitHostPort(c.ControlListen); e != nil || c.ControlListen == c.Listen {
			return errors.New("controlListen must be a different host:port")
		}
	}
	u, e := parseURL(c.PublicURL, true)
	if e != nil {
		return fmt.Errorf("publicUrl: %w", e)
	}
	c.PublicURL = u.String()
	if !validSecret(c.ControlKey) {
		return errors.New("controlKey must be 32..4096 bytes")
	}
	if c.StateDir == "" {
		return errors.New("stateDir required")
	}
	c.StateDir, e = filepath.Abs(c.StateDir)
	if e != nil {
		return e
	}
	// webRoot is accepted only for old configuration compatibility. No local
	// business frontend is opened or served, even if that path no longer exists.
	if c.ClientIP != nil {
		normalized, err := normalizeClientIP(*c.ClientIP)
		if err != nil {
			return err
		}
		c.ClientIP = &normalized
	}
	if (c.TLSCert == "") != (c.TLSKey == "") {
		return errors.New("tlsCert and tlsKey must be supplied together")
	}
	return nil
}
