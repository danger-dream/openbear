package node

import (
	"context"
	"crypto/sha256"
	"crypto/tls"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

// build is the running program's sha256, which tells two builds of one
// version apart. An update starts a new process, so it is read once.
func (a *Agent) build() string {
	a.buildOnce.Do(func() {
		exe, err := a.executable()
		if err != nil {
			return
		}
		f, err := os.Open(exe)
		if err != nil {
			return
		}
		defer f.Close()
		sum := sha256.New()
		if _, err = io.Copy(sum, f); err == nil {
			a.buildHash = hex.EncodeToString(sum.Sum(nil))
		}
	})
	return a.buildHash
}

func (a *Agent) executable() (string, error) {
	if a.exe != "" {
		return a.exe, nil
	}
	exe, err := os.Executable()
	if err != nil {
		return "", err
	}
	return filepath.EvalSymlinks(exe)
}

// BinaryName is this platform's build as the controller names it.
func BinaryName() string {
	name := "openbear-node-" + runtime.GOOS + "-" + runtime.GOARCH
	if runtime.GOOS == "windows" {
		name += ".exe"
	}
	return name
}

type updateParams struct {
	Version string `json:"version"`
	Name    string `json:"name"`
	SHA256  string `json:"sha256"`
	Size    int64  `json:"size"`
}

// update downloads the controller's build for this platform from the
// configured controller, checks its size, hash and that it runs, then
// replaces this executable. The caller restarts into it after replying.
func (a *Agent) update(ctx context.Context, raw json.RawMessage) (map[string]any, error) {
	var p updateParams
	if json.Unmarshal(raw, &p) != nil || p.Version == "" || len(p.SHA256) != 64 || p.Size <= 0 || p.Size > 64<<20 {
		return nil, errors.New("invalid_params")
	}
	if p.Name != BinaryName() {
		return nil, errors.New("wrong_platform")
	}
	if !a.updateMu.TryLock() {
		return nil, errors.New("node_busy")
	}
	defer a.updateMu.Unlock()
	if strings.EqualFold(p.SHA256, a.build()) {
		return map[string]any{"from": Version, "to": Version, "changed": false}, nil
	}
	exe, err := a.executable()
	if err != nil {
		return nil, errors.New("executable_unknown")
	}
	// The address in use: a node on its backup cannot reach the primary.
	c, _ := a.current()
	base, err := ControllerURL(c.URL)
	if err != nil {
		return nil, err
	}
	ctx, cancel := context.WithTimeout(ctx, 5*time.Minute)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, endpoint(base, "/api/server-nodes/v1/download/"+p.Name, false), nil)
	req.Header.Set("User-Agent", "openbear-node/"+Version)
	transport := &http.Transport{TLSClientConfig: &tls.Config{MinVersion: tls.VersionTLS12}}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	resp, err := client.Do(req)
	if err != nil {
		return nil, errors.New("download_failed")
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, errors.New("download_failed")
	}
	next := stagingPath(exe)
	f, err := os.OpenFile(next, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o755)
	if err != nil {
		return nil, fileError(err)
	}
	sum := sha256.New()
	n, err := io.Copy(io.MultiWriter(f, sum), io.LimitReader(resp.Body, p.Size+1))
	if err == nil {
		err = f.Sync()
	}
	f.Close()
	if err != nil {
		os.Remove(next)
		// Writing the staging file failed (disk full, read-only), or the download broke off.
		var pathErr *os.PathError
		if errors.As(err, &pathErr) {
			return nil, fileError(err)
		}
		return nil, errors.New("download_failed")
	}
	if n != p.Size || hex.EncodeToString(sum.Sum(nil)) != strings.ToLower(p.SHA256) {
		os.Remove(next)
		return nil, errors.New("checksum_mismatch")
	}
	check, cancelCheck := context.WithTimeout(ctx, 10*time.Second)
	defer cancelCheck()
	out, err := exec.CommandContext(check, next, "version").Output()
	if err != nil || !strings.HasPrefix(string(out), "openbear-node "+p.Version+" ") {
		os.Remove(next)
		return nil, errors.New("new_binary_does_not_run")
	}
	if err = replaceExecutable(next, exe); err != nil {
		os.Remove(next)
		return nil, fileError(err)
	}
	return map[string]any{"from": Version, "to": p.Version, "changed": true}, nil
}
