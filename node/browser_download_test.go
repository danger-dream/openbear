package node

import (
	"archive/zip"
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

type zipEntry struct {
	name, body string
	mode       os.FileMode
}

func writeZip(t *testing.T, entries []zipEntry) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "a.zip")
	f, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	w := zip.NewWriter(f)
	for _, e := range entries {
		h := &zip.FileHeader{Name: e.name, Method: zip.Store}
		h.SetMode(e.mode)
		out, err := w.CreateHeader(h)
		if err != nil {
			t.Fatal(err)
		}
		out.Write([]byte(e.body))
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	f.Close()
	return path
}

func TestUnzipKeepsBundleLinks(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("symlinks need a privilege on Windows")
	}
	dest := filepath.Join(t.TempDir(), "out")
	archive := writeZip(t, []zipEntry{
		{name: "App/Versions/1.0/", mode: os.ModeDir | 0o755},
		{name: "App/Versions/1.0/bin", body: "exe", mode: 0o755},
		{name: "App/Versions/Current", body: "1.0", mode: os.ModeSymlink | 0o777},
		{name: "App/bin", body: "Versions/Current/bin", mode: os.ModeSymlink | 0o777},
	})
	if err := unzip(archive, dest); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(filepath.Join(dest, "App", "bin"))
	info, _ := os.Stat(filepath.Join(dest, "App", "Versions", "1.0", "bin"))
	if err != nil || string(data) != "exe" || info.Mode().Perm() != 0o755 {
		t.Fatalf("%q %v %v", data, err, info)
	}
}

func TestUnzipRefusesEverythingLeavingTheDestination(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("symlinks need a privilege on Windows")
	}
	link := os.ModeSymlink | 0o777
	for name, entries := range map[string][]zipEntry{
		"parent path":   {{name: "../evil", body: "x", mode: 0o644}},
		"absolute link": {{name: "l", body: "/etc", mode: link}},
		"parent link":   {{name: "l", body: "../outside", mode: link}},
		// Each target reads as inside; through the first link the second leaves.
		"chained link": {{name: "sub/", mode: os.ModeDir | 0o755}, {name: "sub/up", body: "..", mode: link},
			{name: "l", body: "sub/up/..", mode: link}},
		"write through chained link": {{name: "sub/", mode: os.ModeDir | 0o755}, {name: "sub/up", body: "..", mode: link},
			{name: "l", body: "sub/up/..", mode: link}, {name: "l/evil", body: "x", mode: 0o644}},
		"write through link":     {{name: "d/", mode: os.ModeDir | 0o755}, {name: "l", body: "d", mode: link}, {name: "l/x", body: "x", mode: 0o644}},
		"overwrite through link": {{name: "f", body: "x", mode: 0o644}, {name: "l", body: "f", mode: link}, {name: "l", body: "y", mode: 0o644}},
	} {
		t.Run(name, func(t *testing.T) {
			base := t.TempDir()
			dest := filepath.Join(base, "out")
			if err := unzip(writeZip(t, entries), dest); err == nil {
				t.Fatal("accepted")
			}
			for _, outside := range []string{"evil", "outside"} {
				if _, err := os.Lstat(filepath.Join(base, outside)); err == nil {
					t.Fatalf("%s written outside the destination", outside)
				}
			}
		})
	}
}

func TestReadDevToolsPortOnlyTakesALocalPort(t *testing.T) {
	profile := t.TempDir()
	for content, want := range map[string]string{
		"9222\n/devtools/browser/0a1b-2c3d":               "ws://127.0.0.1:9222/devtools/browser/0a1b-2c3d",
		"9222\r\n/devtools/browser/0a1b\r\n":              "ws://127.0.0.1:9222/devtools/browser/0a1b",
		"1@evil.example:80\n/devtools/browser/x":          "",
		"9222@evil.example\n/devtools/browser/x":          "",
		"80/x\n/devtools/browser/x":                       "",
		"+80\n/devtools/browser/x":                        "",
		"0\n/devtools/browser/x":                          "",
		"65536\n/devtools/browser/x":                      "",
		"9222\n/devtools/browser/x@evil.example":          "",
		"9222\n/devtools/browser/../../json":              "",
		"9222\n/devtools/browser/x?redirect=evil.example": "",
		"9222": "",
	} {
		os.WriteFile(filepath.Join(profile, "DevToolsActivePort"), []byte(content), 0o600)
		if got := readDevToolsPort(profile); got != want {
			t.Fatalf("%q: %q, want %q", content, got, want)
		}
	}
}
