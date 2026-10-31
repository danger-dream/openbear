package transport

import (
	"crypto/tls"
	"crypto/x509"
	"errors"
	"net"
	"os"
	"path/filepath"
	"runtime"
	"sync"
	"testing"
	"time"
)

func TestCertificatePendingActivation(t *testing.T) {
	dir := t.TempDir()
	now := time.Now()
	initial, err := ensureCertificate(dir, now.Add(-61*24*time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	if initial.Pending != nil {
		t.Fatal("new certificate already pending")
	}
	current, err := EnsureCertificate(dir)
	if err != nil {
		t.Fatal(err)
	}
	if current.Active.ID != initial.Active.ID || current.Pending == nil {
		t.Fatal("rotation switched active or did not prepare pending")
	}
	if err := ActivateCertificate(dir, "../outside"); err == nil {
		t.Fatal("unknown generation accepted")
	}
	if err := ActivateCertificate(dir, current.Pending.ID); err != nil {
		t.Fatal(err)
	}
	active, err := EnsureCertificate(dir)
	if err != nil {
		t.Fatal(err)
	}
	if active.Active.ID != current.Pending.ID || active.Pending != nil {
		t.Fatal("activation state incorrect")
	}
	if err := ActivateCertificate(dir, active.Active.ID); err != nil {
		t.Fatal("activation not idempotent")
	}
	key, info, err := activeCertificate(dir)
	if err != nil {
		t.Fatal(err)
	}
	if key.Leaf.PublicKeyAlgorithm != x509.ECDSA || info.NotAfter.Sub(now) < 89*24*time.Hour {
		t.Fatal("wrong certificate algorithm or lifetime")
	}
	if runtime.GOOS != "windows" {
		for _, name := range []string{active.Active.ID + ".pem", "certificates.json", ".lock"} {
			st, err := os.Stat(filepath.Join(dir, name))
			if err != nil || st.Mode().Perm() != 0600 {
				t.Fatalf("permissions %s %v %v", name, st, err)
			}
		}
	}
	if err := os.WriteFile(filepath.Join(dir, "certificates.json"), []byte("broken"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := EnsureCertificate(dir); err == nil {
		t.Fatal("corrupt state silently regenerated")
	}
}

func TestCertificateConcurrentEnsure(t *testing.T) {
	dir := t.TempDir()
	var wg sync.WaitGroup
	results := make(chan string, 8)
	fail := make(chan error, 8)
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			c, err := EnsureCertificate(dir)
			if err != nil {
				fail <- err
			} else {
				results <- c.Active.ID
			}
		}()
	}
	wg.Wait()
	close(results)
	close(fail)
	for err := range fail {
		t.Fatal(err)
	}
	id := ""
	for got := range results {
		if id != "" && id != got {
			t.Fatal("concurrent certificate creation diverged")
		}
		id = got
	}
}

func TestCertificatePinAndValidity(t *testing.T) {
	dir := t.TempDir()
	c, err := EnsureCertificate(dir)
	if err != nil {
		t.Fatal(err)
	}
	cert, _, err := readCertificate(dir, c.Active.ID)
	if err != nil {
		t.Fatal(err)
	}
	cs := tls.ConnectionState{PeerCertificates: []*x509.Certificate{cert.Leaf}}
	pins := pinSet{values: []string{c.Active.FingerprintSHA256}}
	if err := verifyPins(cs, pins); err != nil {
		t.Fatal(err)
	}
	leaf := *cert.Leaf
	leaf.NotAfter = time.Now().Add(-time.Second)
	cs.PeerCertificates = []*x509.Certificate{&leaf}
	if err := verifyPins(cs, pins); !errors.Is(err, ErrSecurity) {
		t.Fatalf("expired certificate accepted: %v", err)
	}
	if err := verifyPins(tls.ConnectionState{}, pins); !errors.Is(err, ErrSecurity) {
		t.Fatal(err)
	}
}

func TestTLSRotationWithOverlappingPins(t *testing.T) {
	dir := t.TempDir()
	old, err := ensureCertificate(dir, time.Now().Add(-61*24*time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	server := newBind(t, Options{ListenTLS: freePort(t, "tcp"), TLSDir: dir})
	openBind(t, server)
	certs, err := EnsureCertificate(dir)
	if err != nil || certs.Pending == nil {
		t.Fatalf("pending %v %v", certs, err)
	}
	key := "tls://" + net.JoinHostPort("127.0.0.1", portString(server.opt.ListenTLS))
	client := newBind(t, Options{})
	client.SetPins(key, []string{old.Active.FingerprintSHA256, certs.Pending.FingerprintSHA256})
	openBind(t, client)
	if _, err := client.ParseEndpoint(key); err != nil {
		t.Fatal(err)
	}
	ep := pathReady(t, client, key)
	if client.Stats()[0].FingerprintSHA256 != old.Active.FingerprintSHA256 {
		t.Fatal("pending certificate used too soon")
	}
	if err = ActivateCertificate(dir, certs.Pending.ID); err != nil {
		t.Fatal(err)
	}
	if err = server.Reconfigure(server.opt); err != nil {
		t.Fatal(err)
	}
	client.mu.Lock()
	current := ep.current
	client.mu.Unlock()
	current.conn.Close()
	waitUntil(t, 3*time.Second, "reconnect with new certificate", func() bool {
		s := client.Stats()
		return len(s) == 1 && s[0].State == "connected" && s[0].FingerprintSHA256 == certs.Pending.FingerprintSHA256
	})
	client.SetPins(key, []string{certs.Pending.FingerprintSHA256})
	if client.Stats()[0].State != "connected" {
		t.Fatal("current pin removal disrupted new generation")
	}
}

func TestReconfigureLoadsOnlyActivatedCertificate(t *testing.T) {
	dir := t.TempDir()
	initial, err := ensureCertificate(dir, time.Now().Add(-61*24*time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	b := newBind(t, Options{ListenTLS: freePort(t, "tcp"), TLSDir: dir})
	openBind(t, b)
	c, err := EnsureCertificate(dir)
	if err != nil || c.Pending == nil {
		t.Fatalf("pending %v %v", c, err)
	}
	b.mu.Lock()
	before := b.run.listeners["tls"].certificateID
	b.mu.Unlock()
	if before != initial.Active.ID {
		t.Fatal("pending was activated on listener")
	}
	if err := ActivateCertificate(dir, c.Pending.ID); err != nil {
		t.Fatal(err)
	}
	if err := b.Reconfigure(b.opt); err != nil {
		t.Fatal(err)
	}
	b.mu.Lock()
	after := b.run.listeners["tls"].certificateID
	b.mu.Unlock()
	if after != c.Pending.ID {
		t.Fatal("active certificate did not reload")
	}
}
