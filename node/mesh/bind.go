package mesh

import (
	"log"
	"path/filepath"
	"slices"
	"strings"
	"sync"

	"golang.zx2c4.com/wireguard/conn"

	"openbear/node/mesh/transport"
)

// meshBind is everything the data plane needs from its transport. newBind
// is the one place that picks it.
type meshBind interface {
	conn.Bind
	// endpoint turns a config endpoint into what ParseEndpoint accepts.
	endpoint(configured string) (string, error)
	// reconfigure applies listeners and reloads the TLS certificate; the
	// config is already validated.
	reconfigure(c *Config) error
	// setPins comes before the endpoint is first given to the device.
	setPins(endpoint string, pins []string)
	// release is called once no peer uses endpoint any more.
	release(endpoint string)
	// certificate is the active TLS certificate ID, if any.
	certificate() string
	// localAddrs maps endpoints as the device reports them to the local
	// address of their path.
	localAddrs() map[string]string
	// dialedStreams maps the TCP and TLS endpoints this node dials and has
	// a connection on to that connection's local address.
	dialedStreams() map[string]string
	// streamConns lists the TCP and TLS connections that are up.
	streamConns() []streamConn
}

func newBind(dir string, c *Config) (meshBind, error) {
	b := &streamBind{dir: dir}
	var err error
	if b.Bind, err = transport.New(b.options(c)); err != nil {
		return nil, err
	}
	if err := b.reconfigure(c); err != nil {
		return nil, err
	}
	return b, nil
}

func resourceProfile(c *Config) string {
	if c.Self.Resources != "" {
		return c.Self.Resources
	}
	return c.Self.Role
}

// streamBind is the transport package's UDP, TCP and TLS bind.
type streamBind struct {
	*transport.Bind
	dir  string
	mu   sync.Mutex
	cert string
}

func (b *streamBind) options(c *Config) transport.Options {
	return transport.Options{ListenUDP: c.Self.Listen.UDP, ListenTCP: c.Self.Listen.TCP, ListenTLS: c.Self.Listen.TLS,
		TLSDir: filepath.Join(b.dir, "tls"), MTU: c.Network.MTU, ResourceProfile: resourceProfile(c), Reflector: c.isRelay() && c.Self.Listen.UDP != 0, UDP: c.wantsUDP(),
		Protocol: protocol, Logf: func(format string, args ...any) { log.Printf("transport: "+format, args...) }}
}

// wantsUDP is whether the node keeps a UDP socket without a UDP listener:
// to learn its reachability and for punched paths.
func (c *Config) wantsUDP() bool {
	if len(c.Reflectors) > 0 || (c.Reachability != nil && len(c.Reachability.STUN) > 0) {
		return true
	}
	for _, l := range c.Links {
		if l.Punch {
			return true
		}
	}
	return c.Entry != nil && slices.ContainsFunc(c.Entry.Candidates, func(x Candidate) bool { return x.Punch })
}

// endpoint keeps the scheme: the transport resolves names itself.
func (b *streamBind) endpoint(configured string) (string, error) {
	if strings.HasPrefix(configured, "mesh-peer://") {
		return configured, nil
	}
	if _, err := parseEndpoint(configured); err != nil {
		return "", err
	}
	return configured, nil
}

func (b *streamBind) reconfigure(c *Config) error {
	if err := b.Reconfigure(b.options(c)); err != nil {
		return err
	}
	id := ""
	if c.Self.Listen.TLS != 0 {
		// The listener has just loaded the active certificate, creating it
		// if needed, so this only reads which one that is.
		certs, err := transport.EnsureCertificate(filepath.Join(b.dir, "tls"))
		if err != nil {
			return err
		}
		id = certs.Active.ID
	}
	b.mu.Lock()
	b.cert = id
	b.mu.Unlock()
	return nil
}

func (b *streamBind) setPins(endpoint string, pins []string) { b.SetPins(endpoint, pins) }

func (b *streamBind) release(endpoint string) {
	if !strings.HasPrefix(endpoint, "mesh-peer://") {
		b.Release(endpoint)
	}
}

func (b *streamBind) certificate() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.cert
}

func (b *streamBind) localAddrs() map[string]string {
	out := map[string]string{}
	for _, s := range b.Stats() {
		if s.LocalAddr != "" {
			out[s.Endpoint] = s.LocalAddr
		}
	}
	return out
}

func (b *streamBind) dialedStreams() map[string]string {
	out := map[string]string{}
	for _, s := range b.Stats() {
		if s.Outbound && s.Transport != "udp" && s.State == "connected" {
			out[s.Endpoint] = s.LocalAddr
		}
	}
	return out
}

// transportOf names the transport of an endpoint as status reports it.
func transportOf(endpoint string) string {
	if scheme, _, ok := strings.Cut(endpoint, "://"); ok {
		return scheme
	}
	if endpoint == "" {
		return ""
	}
	return "udp"
}

func stripScheme(endpoint string) string {
	if _, rest, ok := strings.Cut(endpoint, "://"); ok {
		return rest
	}
	return endpoint
}
