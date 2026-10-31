//go:build windows

package node

import (
	"crypto/ed25519"
	"encoding/base64"
	"os"
	"os/exec"
	"path/filepath"
	"testing"

	"golang.org/x/sys/windows"
)

// Run as SYSTEM or an elevated administrator: setting another owner needs SeRestorePrivilege.
func squat(t *testing.T, dir string) {
	t.Helper()
	enablePrivileges("SeRestorePrivilege")
	users, _ := windows.CreateWellKnownSid(windows.WinBuiltinUsersSid)
	sd, _ := windows.SecurityDescriptorFromString("D:P(A;OICI;FA;;;BU)(A;OICI;FA;;;SY)")
	dacl, _, _ := sd.DACL()
	if err := windows.SetNamedSecurityInfo(dir, windows.SE_FILE_OBJECT,
		windows.OWNER_SECURITY_INFORMATION|windows.DACL_SECURITY_INFORMATION|windows.PROTECTED_DACL_SECURITY_INFORMATION, users, nil, dacl, nil); err != nil {
		t.Skip("cannot set another owner here:", err)
	}
}

func ownerOf(t *testing.T, path string) *windows.SID {
	sd, err := windows.GetNamedSecurityInfo(path, windows.SE_FILE_OBJECT, windows.OWNER_SECURITY_INFORMATION)
	if err != nil {
		t.Fatal(err)
	}
	owner, _, _ := sd.Owner()
	return owner
}

func junction(t *testing.T, link, target string) {
	if out, err := exec.Command("cmd", "/c", "mklink", "/J", link, target).CombinedOutput(); err != nil {
		t.Fatal(err, string(out))
	}
}

func TestADirectoryAUserMadeFirstIsReplacedLinksWithoutTheirTargets(t *testing.T) {
	base := t.TempDir()
	outside := filepath.Join(base, "outside")
	os.MkdirAll(outside, 0o700)
	os.WriteFile(filepath.Join(outside, "keep.txt"), []byte("x"), 0o600)
	dir := filepath.Join(base, "OpenBear")
	os.MkdirAll(filepath.Join(dir, "node", "browser"), 0o700)
	os.WriteFile(filepath.Join(dir, "node", "browser", "Preferences"), []byte("planted"), 0o600)
	junction(t, filepath.Join(dir, "node", "link"), outside)
	squat(t, filepath.Join(dir, "node", "browser"))
	squat(t, dir)

	if err := ownedDir(dir, false); err != nil {
		t.Fatal(err)
	}
	entries, _ := os.ReadDir(dir)
	if len(entries) != 0 {
		t.Fatalf("left behind: %v", entries)
	}
	if owner := ownerOf(t, dir); !owner.IsWellKnown(windows.WinBuiltinAdministratorsSid) && !owner.IsWellKnown(windows.WinLocalSystemSid) {
		t.Fatalf("owner %v", owner)
	}
	if _, err := os.Stat(filepath.Join(outside, "keep.txt")); err != nil {
		t.Fatal("the junction's target lost its file:", err)
	}
}

func TestADirectoryThatIsAJunctionIsUnlinkedAndMadeReal(t *testing.T) {
	base := t.TempDir()
	outside := filepath.Join(base, "outside")
	os.MkdirAll(outside, 0o700)
	os.WriteFile(filepath.Join(outside, "keep.txt"), []byte("x"), 0o600)
	dir := filepath.Join(base, "OpenBear")
	junction(t, dir, outside)

	// Also when a node's config is found through it.
	if err := ownedDir(dir, true); err != nil {
		t.Fatal(err)
	}
	info, err := os.Lstat(dir)
	if err != nil || !info.IsDir() || reparsePoint(info) {
		t.Fatalf("not a plain directory: %v %v", info, err)
	}
	if _, err := os.Stat(filepath.Join(outside, "keep.txt")); err != nil {
		t.Fatal("the junction's target lost its file:", err)
	}
}

func TestTheNodesOwnDirectoryKeepsWhatItHolds(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "node")
	os.MkdirAll(filepath.Join(dir, "mesh", "keys"), 0o700)
	os.WriteFile(filepath.Join(dir, "mesh", "keys", "wg.key"), []byte("k"), 0o600)
	if err := ownedDir(dir, false); err != nil {
		t.Fatal(err)
	}
	if data, err := os.ReadFile(filepath.Join(dir, "mesh", "keys", "wg.key")); err != nil || string(data) != "k" {
		t.Fatal("its contents went:", err)
	}
}

// From the 10-30 check: an install made by an administrator's own account left
// the directories owned by that account, and they were deleted with the config
// and the mesh state. Holding this node's config, they are taken over and kept.
func TestTheInstalledLayoutHoldingTheNodesConfigIsTakenOverAndKept(t *testing.T) {
	top := filepath.Join(t.TempDir(), "OpenBear")
	dir := filepath.Join(top, "node")
	os.MkdirAll(filepath.Join(dir, "mesh", "keys"), 0o700)
	os.WriteFile(filepath.Join(dir, "mesh", "keys", "wg.key"), []byte("k"), 0o600)
	c := Config{NodeID: "n1", PrivateKey: base64.StdEncoding.EncodeToString(make([]byte, ed25519.SeedSize))}
	if err := writeConfig(filepath.Join(dir, "config.json"), c); err != nil {
		t.Fatal(err)
	}
	squat(t, dir)
	squat(t, top)
	if err := secureLayout(dir); err != nil {
		t.Fatal(err)
	}
	if data, err := os.ReadFile(filepath.Join(dir, "mesh", "keys", "wg.key")); err != nil || string(data) != "k" {
		t.Fatal("its contents went:", err)
	}
	if !nodeConfig(filepath.Join(dir, "config.json")) {
		t.Fatal("the config went")
	}
	for _, d := range []string{top, dir} {
		if owner := ownerOf(t, d); !owner.IsWellKnown(windows.WinBuiltinAdministratorsSid) && !owner.IsWellKnown(windows.WinLocalSystemSid) {
			t.Fatalf("%s owner %v", d, owner)
		}
	}
}

// From the 10-30 check: a config path of the user's own (enroll --config in the
// downloads folder) had its directory emptied. Only its access is narrowed.
func TestACustomConfigDirectoryKeepsItsFiles(t *testing.T) {
	dir := t.TempDir()
	os.WriteFile(filepath.Join(dir, "notes.txt"), []byte("mine"), 0o600)
	squat(t, dir)
	path := filepath.Join(dir, "config.json")
	if err := SecureConfig(path, Config{NodeID: "n1"}); err != nil {
		t.Fatal(err)
	}
	if data, err := os.ReadFile(filepath.Join(dir, "notes.txt")); err != nil || string(data) != "mine" {
		t.Fatal("the user's file went:", err)
	}
	if data, err := os.ReadFile(path); err != nil || len(data) < 10 {
		t.Fatalf("config not written: %q %v", data, err)
	}
}
