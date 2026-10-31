package transport

import (
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
	"fmt"
	"math/big"
	"os"
	"path/filepath"
	"time"
)

type Certificate struct {
	ID, FingerprintSHA256 string
	NotBefore, NotAfter   time.Time
}

type Certificates struct {
	Active  Certificate
	Pending *Certificate
}

type certificateState struct {
	Active  string `json:"active"`
	Pending string `json:"pending,omitempty"`
}

func EnsureCertificate(dir string) (Certificates, error) {
	return ensureCertificate(dir, time.Now())
}

func ensureCertificate(dir string, now time.Time) (Certificates, error) {
	var out Certificates
	unlock, err := lockCertificates(dir)
	if err != nil {
		return out, err
	}
	defer unlock()
	state, err := readCertificateState(dir)
	if err != nil && !os.IsNotExist(err) {
		return out, err
	}
	changed := false
	if os.IsNotExist(err) {
		cert, err := createCertificate(dir, now)
		if err != nil {
			return out, err
		}
		state.Active, changed = cert.ID, true
	}
	_, out.Active, err = readCertificate(dir, state.Active)
	if err != nil {
		return out, err
	}
	if state.Pending != "" {
		_, c, err := readCertificate(dir, state.Pending)
		if err != nil {
			return out, err
		}
		out.Pending = &c
	}
	if out.Active.NotAfter.Sub(now) <= 30*24*time.Hour && (out.Pending == nil || !out.Pending.NotAfter.After(now)) {
		cert, err := createCertificate(dir, now)
		if err != nil {
			return out, err
		}
		state.Pending, out.Pending, changed = cert.ID, &cert, true
	}
	if changed {
		if err := writeCertificateState(dir, state); err != nil {
			return out, err
		}
	}
	return out, nil
}

func ActivateCertificate(dir, id string) error {
	unlock, err := lockCertificates(dir)
	if err != nil {
		return err
	}
	defer unlock()
	state, err := readCertificateState(dir)
	if err != nil {
		return err
	}
	if id == "" || (id != state.Active && id != state.Pending) {
		return errors.New("certificate is not the active or pending generation")
	}
	_, cert, err := readCertificate(dir, id)
	if err != nil {
		return err
	}
	now := time.Now()
	if now.Before(cert.NotBefore) || !now.Before(cert.NotAfter) {
		return errors.New("certificate is not currently valid")
	}
	if id == state.Active {
		return nil
	}
	return writeCertificateState(dir, certificateState{Active: id})
}

func activeCertificate(dir string) (*tls.Certificate, Certificate, error) {
	if _, err := EnsureCertificate(dir); err != nil {
		return nil, Certificate{}, err
	}
	unlock, err := lockCertificates(dir)
	if err != nil {
		return nil, Certificate{}, err
	}
	defer unlock()
	state, err := readCertificateState(dir)
	if err != nil {
		return nil, Certificate{}, err
	}
	return readCertificate(dir, state.Active)
}

func lockCertificates(dir string) (func(), error) {
	if dir == "" {
		return nil, errors.New("TLSDir is required")
	}
	if err := os.MkdirAll(dir, 0700); err != nil {
		return nil, err
	}
	if err := securePath(dir, true); err != nil {
		return nil, err
	}
	path := filepath.Join(dir, ".lock")
	if err := rejectSymlink(path); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, err
	}
	if err = securePath(path, false); err == nil {
		err = lockFile(f)
	}
	if err != nil {
		f.Close()
		return nil, err
	}
	return func() { unlockFile(f); f.Close() }, nil
}

func readCertificateState(dir string) (certificateState, error) {
	var state certificateState
	path := filepath.Join(dir, "certificates.json")
	if err := rejectSymlink(path); err != nil {
		return state, err
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return state, err
	}
	if len(raw) > 4096 || json.Unmarshal(raw, &state) != nil || !validCertificateID(state.Active) || (state.Pending != "" && !validCertificateID(state.Pending)) {
		return state, errors.New("invalid certificate state")
	}
	return state, securePath(path, false)
}

func validCertificateID(id string) bool {
	b, err := hex.DecodeString(id)
	return err == nil && len(b) == 16
}

func readCertificate(dir, id string) (*tls.Certificate, Certificate, error) {
	var info Certificate
	if !validCertificateID(id) {
		return nil, info, errors.New("invalid certificate ID")
	}
	path := filepath.Join(dir, id+".pem")
	if err := rejectSymlink(path); err != nil {
		return nil, info, err
	}
	if err := securePath(path, false); err != nil {
		return nil, info, err
	}
	buf, err := os.ReadFile(path)
	if err != nil {
		return nil, info, err
	}
	if len(buf) > 32768 {
		return nil, info, errors.New("certificate bundle too large")
	}
	cert, err := tls.X509KeyPair(buf, buf)
	if err != nil {
		return nil, info, errors.New("invalid certificate bundle")
	}
	leaf, err := x509.ParseCertificate(cert.Certificate[0])
	if err != nil {
		return nil, info, errors.New("invalid leaf certificate")
	}
	key, ok := cert.PrivateKey.(*ecdsa.PrivateKey)
	if !ok || key.Curve != elliptic.P256() || leaf.CheckSignature(leaf.SignatureAlgorithm, leaf.RawTBSCertificate, leaf.Signature) != nil {
		return nil, info, errors.New("certificate must be self-signed ECDSA P-256")
	}
	cert.Leaf = leaf
	h := sha256.Sum256(leaf.Raw)
	info = Certificate{ID: id, FingerprintSHA256: hex.EncodeToString(h[:]), NotBefore: leaf.NotBefore, NotAfter: leaf.NotAfter}
	return &cert, info, nil
}

func createCertificate(dir string, now time.Time) (Certificate, error) {
	var out Certificate
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return out, err
	}
	serial, err := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 128))
	if err != nil {
		return out, err
	}
	var id [16]byte
	if _, err := rand.Read(id[:]); err != nil {
		return out, err
	}
	template := &x509.Certificate{SerialNumber: serial, Subject: pkix.Name{CommonName: "OpenBear mesh relay"}, NotBefore: now.Add(-5 * time.Minute), NotAfter: now.Add(90 * 24 * time.Hour), KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		return out, err
	}
	priv, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		return out, err
	}
	bundle := append(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: priv})...)
	name := hex.EncodeToString(id[:])
	path := filepath.Join(dir, name+".pem")
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return out, err
	}
	if err = securePath(path, false); err == nil {
		err = writeAll(f, bundle)
	}
	if err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err == nil {
		err = closeErr
	}
	if err != nil {
		return out, fmt.Errorf("write certificate: %w", err)
	}
	_, out, err = readCertificate(dir, name)
	return out, err
}

func writeCertificateState(dir string, state certificateState) error {
	raw, _ := json.Marshal(state)
	f, err := os.CreateTemp(dir, ".certificates-*")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	if err = securePath(f.Name(), false); err == nil {
		err = writeAll(f, raw)
	}
	if err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	return replaceFile(f.Name(), filepath.Join(dir, "certificates.json"))
}

func rejectSymlink(path string) error {
	info, err := os.Lstat(path)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	if info.Mode()&os.ModeSymlink != 0 {
		return errors.New("symbolic links are not allowed in TLS state")
	}
	return nil
}
