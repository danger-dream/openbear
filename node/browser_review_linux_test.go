//go:build linux

package node

import (
	"errors"
	"os"
	"os/user"
	"path/filepath"
	"strings"
	"testing"
)

func TestBrowserMissingAccountNeverFallsBackToRoot(t *testing.T) {
	t.Setenv("OPENBEAR_BROWSER_USER", "")
	missing := func(string) (*user.User, error) { return nil, errors.New("missing") }
	refuse := func(string, ...string) error { return errors.New("read-only system") }
	if credential, err := browserCredentialFor(t.TempDir(), missing, refuse); err == nil || credential != nil || !strings.HasPrefix(err.Error(), "browser_account_unavailable") {
		t.Fatalf("missing account: %v %v", credential, err)
	}
	lookup := func(name string) (*user.User, error) {
		if name == "nobody" {
			return &user.User{Uid: "65534", Gid: "65534"}, nil
		}
		return missing(name)
	}
	if credential, err := browserCredentialFor(t.TempDir(), lookup, refuse); err != nil || credential.Uid != 65534 {
		t.Fatalf("nobody: %v %v", credential, err)
	}
	for _, uid := range []string{"0", "-1", "invalid"} {
		if _, err := accountCredential(&user.User{Uid: uid, Gid: "1"}); err == nil {
			t.Fatalf("accepted uid %s", uid)
		}
	}
	t.Setenv("OPENBEAR_BROWSER_USER", "custom")
	lookup = func(name string) (*user.User, error) {
		if name != "custom" {
			t.Fatal(name)
		}
		return &user.User{Uid: "1234", Gid: "5678"}, nil
	}
	if credential, err := browserCredentialFor(t.TempDir(), lookup, refuse); err != nil || credential.Uid != 1234 || credential.Gid != 5678 {
		t.Fatalf("explicit account: %v %v", credential, err)
	}
}

func TestBrowserProfileCleanupMatchesUIDAndExactProfile(t *testing.T) {
	proc := t.TempDir()
	profile := filepath.Join(t.TempDir(), "profile")
	if err := os.Symlink("/usr/bin/chromium", filepath.Join(proc, "exe")); err != nil {
		t.Fatal(err)
	}
	for _, args := range []string{"chromium\x00--user-data-dir=" + profile + "\x00", "chromium\x00--user-data-dir\x00" + profile + "\x00"} {
		os.WriteFile(filepath.Join(proc, "cmdline"), []byte(args), 0o600)
		if !browserOnProfile(proc, profile, os.Geteuid()) {
			t.Fatal("own browser not recognized")
		}
		if browserOnProfile(proc, profile, os.Geteuid()+1) {
			t.Fatal("other account matched")
		}
		if browserOnProfile(proc, profile+"-other", os.Geteuid()) {
			t.Fatal("other profile matched")
		}
	}
	os.WriteFile(filepath.Join(proc, "cmdline"), []byte("chromium\x00--type=renderer\x00"), 0o600)
	if browserOnProfile(proc, profile, os.Geteuid()) {
		t.Fatal("unrelated renderer matched")
	}
}

func TestSharedDownloadsRejectsUnexpectedOwnerWithoutChangingIt(t *testing.T) {
	if os.Geteuid() != 0 {
		t.Skip("changing owner needs root")
	}
	path := filepath.Join(t.TempDir(), "shared")
	if err := os.Mkdir(path, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Chown(path, 65534, 65534); err != nil {
		t.Fatal(err)
	}
	if err := prepareSharedDownloads(path); err == nil || err.Error() != "browser_download_directory_owner_invalid" {
		t.Fatalf("owner check: %v", err)
	}
	info, err := os.Stat(path)
	if err != nil || info.Mode().Perm() != 0o700 {
		t.Fatalf("unexpected directory changed: %v %v", info, err)
	}
}
