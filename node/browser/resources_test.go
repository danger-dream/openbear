package browser

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"
)

// The embedded page scripts must stay byte-identical to the Python engine's
// copies so both engines produce the same snapshots and refs.
func TestEmbeddedResourcesMatchPythonEngine(t *testing.T) {
	dir := filepath.Join("..", "..", "app", "browser", "pyworker", "resources")
	if _, err := os.Stat(dir); err != nil {
		t.Skip("Python engine resources not present")
	}
	for name, embedded := range ResourceFiles() {
		original, err := os.ReadFile(filepath.Join(dir, name))
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if !bytes.Equal(original, embedded) {
			t.Fatalf("%s differs from the Python engine copy", name)
		}
	}
	entries, _ := os.ReadDir(dir)
	for _, entry := range entries {
		if _, ok := ResourceFiles()[entry.Name()]; !ok && !entry.IsDir() {
			t.Fatalf("%s is not embedded", entry.Name())
		}
	}
	rows, index := keys()
	if len(rows) == 0 || index["Enter"] == nil {
		t.Fatal("keys.json not loaded")
	}
}
