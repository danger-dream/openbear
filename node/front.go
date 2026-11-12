package node

// A front (前置机) reaches nothing beyond its exits. Each exit serves its
// fronts' access to the controller on FrontPort: TLS with a self-signed
// certificate the fronts pin, forwarding only the node API to the exit's own
// address of the controller. Sessions are sealed end to end (secure.go) and
// downloads checked by hash, so the exit only ever relays what it cannot read
// or change unnoticed.

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"math/big"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	frontAPI = "/api/server-nodes/v1/"
	maxPins  = 2
	// The certificate is what fronts pin: it lasts as long as the exit's config.
	frontCertLife     = 30 * 365 * 24 * time.Hour
	tunnelDialTimeout = 10 * time.Second
)

var errPinMismatch = errors.New("the server's certificate is not the pinned one")

func validPin(p string) bool {
	raw, err := hex.DecodeString(p)
	return err == nil && len(raw) == sha256.Size && p == strings.ToLower(p)
}

func cleanPins(raw []string) ([]string, error) {
	if len(raw) > maxPins {
		return nil, errors.New("at most two pins per address")
	}
	out := make([]string, 0, len(raw))
	for _, p := range raw {
		p = strings.ToLower(strings.TrimSpace(p))
		if !validPin(p) {
			return nil, errors.New("a pin must be 64 hex digits")
		}
		out = append(out, p)
	}
	return out, nil
}

func checkPins(pins map[string][]string) error {
	if len(pins) > 2 {
		return errors.New("config pins are invalid")
	}
	for address, list := range pins {
		u, err := ControllerURL(address)
		if err != nil || u.Scheme != "https" || len(list) == 0 {
			return errors.New("config pins are invalid")
		}
		if _, err = cleanPins(list); err != nil {
			return errors.New("config pins are invalid")
		}
	}
	return nil
}

// setPins keeps pins only for the configured addresses.
func (c *Config) setPins(urlPins, backupPins []string) {
	c.Pins = nil
	for address, pins := range map[string][]string{c.URL: urlPins, c.Backup: backupPins} {
		if address != "" && len(pins) > 0 && strings.HasPrefix(address, "https://") {
			if c.Pins == nil {
				c.Pins = map[string][]string{}
			}
			c.Pins[address] = slices.Clone(pins)
		}
	}
}

// tlsFor is how an https address is checked: by its pins when it has some,
// else against the system's roots.
func (c Config) tlsFor(u *url.URL) *tls.Config {
	if pins := c.Pins[u.String()]; len(pins) > 0 {
		return pinnedTLS(pins)
	}
	return &tls.Config{ServerName: u.Hostname(), MinVersion: tls.VersionTLS12}
}

func pinnedTLS(pins []string) *tls.Config {
	return &tls.Config{MinVersion: tls.VersionTLS13, InsecureSkipVerify: true,
		VerifyPeerCertificate: func(raw [][]byte, _ [][]*x509.Certificate) error {
			if len(raw) == 0 {
				return errPinMismatch
			}
			sum := sha256.Sum256(raw[0])
			if !slices.Contains(pins, hex.EncodeToString(sum[:])) {
				return errPinMismatch
			}
			return nil
		}}
}

// frontAccess is an exit's listener for its fronts.
type frontAccess struct {
	mu          sync.Mutex
	port        int
	server      *http.Server
	fingerprint string
	err         string
}

func (a *Agent) frontDir() string {
	if a.ConfigPath == "" {
		return ""
	}
	return filepath.Join(filepath.Dir(a.ConfigPath), "front")
}

// frontCertificate is the exit's certificate, made once and kept with its config.
func frontCertificate(dir string) (tls.Certificate, string, error) {
	certPath, keyPath := filepath.Join(dir, "cert.pem"), filepath.Join(dir, "key.pem")
	if dir != "" {
		if cert, err := tls.LoadX509KeyPair(certPath, keyPath); err == nil {
			sum := sha256.Sum256(cert.Certificate[0])
			return cert, hex.EncodeToString(sum[:]), nil
		}
	}
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return tls.Certificate{}, "", err
	}
	serial, _ := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 120))
	now := time.Now()
	template := &x509.Certificate{SerialNumber: serial, Subject: pkix.Name{CommonName: "openbear front access"},
		NotBefore: now.Add(-time.Hour), NotAfter: now.Add(frontCertLife),
		KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		return tls.Certificate{}, "", err
	}
	keyDER, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		return tls.Certificate{}, "", err
	}
	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER})
	if dir != "" {
		if err = os.MkdirAll(dir, 0o700); err != nil {
			return tls.Certificate{}, "", err
		}
		if err = protectDir(dir); err != nil {
			return tls.Certificate{}, "", err
		}
		if err = os.WriteFile(keyPath, keyPEM, 0o600); err != nil {
			return tls.Certificate{}, "", err
		}
		if err = os.WriteFile(certPath, certPEM, 0o600); err != nil {
			return tls.Certificate{}, "", err
		}
	}
	cert, err := tls.X509KeyPair(certPEM, keyPEM)
	if err != nil {
		return tls.Certificate{}, "", err
	}
	sum := sha256.Sum256(der)
	return cert, hex.EncodeToString(sum[:]), nil
}

// serveFront listens for fronts on port, or stops listening for port 0.
func (a *Agent) serveFront(port int) error {
	f := &a.front
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.server != nil && f.port == port {
		return nil
	}
	if port == 0 {
		if f.server != nil {
			f.server.Close()
			f.server = nil
		}
		f.port, f.err = 0, ""
		return nil
	}
	cert, fingerprint, err := frontCertificate(a.frontDir())
	if err != nil {
		f.err = "certificate: " + err.Error()
		return errors.New("front_certificate_failed")
	}
	ln, err := net.Listen("tcp", net.JoinHostPort("", strconv.Itoa(port)))
	if err != nil {
		f.err = err.Error()
		return errors.New("front_listen_failed")
	}
	server := &http.Server{Handler: a.frontProxy(), ReadHeaderTimeout: 15 * time.Second,
		TLSConfig: &tls.Config{MinVersion: tls.VersionTLS13, Certificates: []tls.Certificate{cert}}}
	// Only retire the working listener after the replacement is bound.
	if f.server != nil {
		f.server.Close()
	}
	f.server, f.port, f.fingerprint, f.err = server, port, fingerprint, ""
	go server.ServeTLS(ln, "", "")
	return nil
}

func (a *Agent) frontStatus() map[string]any {
	f := &a.front
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.port == 0 {
		return nil
	}
	return map[string]any{"port": f.port, "fingerprint": f.fingerprint, "listening": f.server != nil, "error": f.err}
}

// frontProxy forwards the node API to the address this node itself uses, the
// way its own session goes (primary or backup).
func (a *Agent) frontProxy() http.Handler {
	var mu sync.Mutex
	transports := map[string]*http.Transport{}
	transport := func(c Config, base *url.URL) *http.Transport {
		mu.Lock()
		defer mu.Unlock()
		key := base.String() + "|" + strings.Join(c.Pins[base.String()], ",")
		if t := transports[key]; t != nil {
			return t
		}
		for _, t := range transports {
			t.CloseIdleConnections()
		}
		t := &http.Transport{TLSClientConfig: c.tlsFor(base), DialContext: (&net.Dialer{Timeout: 15 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
			TLSHandshakeTimeout: 15 * time.Second, MaxIdleConnsPerHost: 4, IdleConnTimeout: 90 * time.Second}
		transports = map[string]*http.Transport{key: t}
		return t
	}
	proxy := &httputil.ReverseProxy{
		Rewrite: func(r *httputil.ProxyRequest) {
			c, _ := a.current()
			base, _ := ControllerURL(c.URL)
			r.SetURL(base)
			r.Out = r.Out.WithContext(context.WithValue(r.Out.Context(), frontUpstream{}, frontTarget{c, base}))
		},
		Transport: roundTripper(func(r *http.Request) (*http.Response, error) {
			target, _ := r.Context().Value(frontUpstream{}).(frontTarget)
			if target.base == nil {
				return nil, errors.New("no controller address")
			}
			return transport(target.config, target.base).RoundTrip(r)
		}),
		FlushInterval: -1,
		ErrorHandler: func(w http.ResponseWriter, _ *http.Request, _ error) {
			w.WriteHeader(http.StatusBadGateway)
		},
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasPrefix(r.URL.Path, frontAPI) {
			http.NotFound(w, r)
			return
		}
		proxy.ServeHTTP(w, r)
	})
}

type frontUpstream struct{}

type frontTarget struct {
	config Config
	base   *url.URL
}

type roundTripper func(*http.Request) (*http.Response, error)

func (f roundTripper) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

// frontServe is the controller's call that makes this node an exit (port) or not (0).
func (a *Agent) frontServe(raw json.RawMessage) (map[string]any, error) {
	var p struct {
		Port *int `json:"port"`
	}
	if json.Unmarshal(raw, &p) != nil || p.Port == nil || *p.Port < 0 || *p.Port > 65535 {
		return nil, errors.New("invalid_params")
	}
	c := a.config()
	if err := a.serveFront(*p.Port); err != nil {
		return nil, err
	}
	if c.FrontPort != *p.Port && a.ConfigPath != "" {
		previous := c.FrontPort
		c.FrontPort = *p.Port
		if err := SaveConfig(a.ConfigPath, c); err != nil {
			a.serveFront(previous)
			return nil, errors.New("config_write_failed")
		}
	}
	a.mu.Lock()
	a.Config.FrontPort = *p.Port
	a.mu.Unlock()
	status := a.frontStatus()
	if status == nil {
		status = map[string]any{"port": 0}
	}
	return status, nil
}

// tcpConsole is a terminal that is a TCP connection: the controller reaching
// host:port through this node, as a front's SSH through its exit.
type tcpConsole struct{ net.Conn }

func (tcpConsole) Resize(int, int) error { return nil }
func (c tcpConsole) Hangup()             { c.Conn.Close() }
func (tcpConsole) Wait()                 {}

func dialTunnel(ctx context.Context, address string) (console, error) {
	host, port, err := net.SplitHostPort(address)
	if err != nil || host == "" {
		return nil, errors.New("invalid_address")
	}
	if n, err := strconv.Atoi(port); err != nil || n < 1 || n > 65535 {
		return nil, errors.New("invalid_address")
	}
	ctx, cancel := context.WithTimeout(ctx, tunnelDialTimeout)
	defer cancel()
	conn, err := (&net.Dialer{KeepAlive: 30 * time.Second}).DialContext(ctx, "tcp", address)
	if err != nil {
		return nil, err
	}
	return tcpConsole{conn}, nil
}
