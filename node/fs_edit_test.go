//go:build unix

package node

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func editCall(t *testing.T, u *uploads, path string, edits ...map[string]any) (map[string]any, error) {
	t.Helper()
	raw, _ := json.Marshal(map[string]any{"path": path, "edits": edits})
	v, err := u.edit(raw)
	if err != nil {
		return nil, err
	}
	out, _ := json.Marshal(v)
	var m map[string]any
	json.Unmarshal(out, &m)
	return m, nil
}

func edit(old, new string) map[string]any { return map[string]any{"old": old, "new": new} }

func TestEditReplacesEveryEditAtomicallyKeepingMode(t *testing.T) {
	u := newUploads()
	path := filepath.Join(t.TempDir(), "app.conf")
	lines := []string{}
	for i := 1; i <= 20; i++ {
		lines = append(lines, "line "+strings.Repeat("x", i%3))
	}
	lines[2], lines[15] = "port = 80", "debug = false"
	os.WriteFile(path, []byte(strings.Join(lines, "\n")+"\n"), 0o640)
	r, err := editCall(t, u, path, edit("port = 80", "port = 8080"), edit("debug = false", "debug = true\nverbose = 1"))
	if err != nil || r["changed"] != true || r["replacements"].(float64) != 2 {
		t.Fatalf("edit: %v %v", r, err)
	}
	b, _ := os.ReadFile(path)
	if !strings.Contains(string(b), "port = 8080\n") || !strings.Contains(string(b), "debug = true\nverbose = 1\n") || strings.Count(string(b), "\n") != 21 {
		t.Fatalf("content: %q", b)
	}
	if st, _ := os.Stat(path); st.Mode().Perm() != 0o640 {
		t.Fatalf("mode not kept: %v", st.Mode())
	}
	diff := r["diff"].(string)
	for _, want := range []string{"--- " + path, "@@ -1,6 +1,6 @@", "-port = 80\n+port = 8080\n", "@@ -13,7 +13,8 @@", "-debug = false\n+debug = true\n+verbose = 1\n"} {
		if !strings.Contains(diff, want) {
			t.Fatalf("diff lacks %q:\n%s", want, diff)
		}
	}
	entries, _ := os.ReadDir(filepath.Dir(path))
	if len(entries) != 1 {
		t.Fatalf("temp file left behind: %v", entries)
	}
}

func TestEditWritesNothingUnlessEveryEditApplies(t *testing.T) {
	u := newUploads()
	path := filepath.Join(t.TempDir(), "f.txt")
	original := "alpha\nbeta\nbeta\ngamma delta\n"
	os.WriteFile(path, []byte(original), 0o600)
	cases := []struct {
		edits  []map[string]any
		reason string
		edit   float64
	}{
		{[]map[string]any{edit("alpha", "A"), edit("missing", "x")}, "not_found", 2},
		{[]map[string]any{edit("beta", "B"), edit("alpha", "A")}, "multiple", 1},
		{[]map[string]any{edit("gamma delta", "x"), edit("delta", "y")}, "overlap", 2},
	}
	for _, c := range cases {
		r, err := editCall(t, u, path, c.edits...)
		if err != nil || r["changed"] != false {
			t.Fatalf("%v: %v %v", c.reason, r, err)
		}
		failures := r["failures"].([]any)
		first := failures[0].(map[string]any)
		if len(failures) != 1 || first["reason"] != c.reason || first["edit"].(float64) != c.edit {
			t.Fatalf("%s: %v", c.reason, failures)
		}
		if c.reason == "multiple" && (first["count"].(float64) != 2 || len(first["lines"].([]any)) != 2) {
			t.Fatalf("multiple must say how many and where: %v", first)
		}
		if b, _ := os.ReadFile(path); string(b) != original {
			t.Fatalf("%s: file changed to %q", c.reason, b)
		}
	}
	r, err := editCall(t, u, path, map[string]any{"old": "beta", "new": "B", "replaceAll": true})
	if err != nil || r["replacements"].(float64) != 2 {
		t.Fatalf("replaceAll: %v %v", r, err)
	}
	if b, _ := os.ReadFile(path); string(b) != "alpha\nB\nB\ngamma delta\n" {
		t.Fatalf("replaceAll: %q", b)
	}
}

func TestEditMatchesLFTextInACRLFFile(t *testing.T) {
	u := newUploads()
	path := filepath.Join(t.TempDir(), "win.ini")
	os.WriteFile(path, []byte("[a]\r\nkey=1\r\nother=2\r\n"), 0o644)
	r, err := editCall(t, u, path, edit("key=1\nother=2", "key=3\nother=4\nnew=5"))
	if err != nil || r["changed"] != true || r["lineEndings"] != "crlf" {
		t.Fatalf("crlf edit: %v %v", r, err)
	}
	if b, _ := os.ReadFile(path); string(b) != "[a]\r\nkey=3\r\nother=4\r\nnew=5\r\n" {
		t.Fatalf("crlf content: %q", b)
	}
	if diff := r["diff"].(string); strings.Contains(diff, "\r") || !strings.Contains(diff, "+new=5\n") {
		t.Fatalf("diff: %q", diff)
	}
}

func TestEditRefusesWhatIsNotAUTF8TextFile(t *testing.T) {
	u := newUploads()
	dir := t.TempDir()
	for name, data := range map[string][]byte{"bin": {'a', 0, 'b'}, "latin1": {'c', 'a', 'f', 0xe9}} {
		path := filepath.Join(dir, name)
		os.WriteFile(path, data, 0o644)
		if _, err := editCall(t, u, path, edit("a", "b")); err == nil || err.Error() != "not_text" {
			t.Fatalf("%s: %v", name, err)
		}
	}
	big := filepath.Join(dir, "big")
	os.WriteFile(big, make([]byte, fileChunkMax+1), 0o644)
	if _, err := editCall(t, u, big, edit("a", "b")); err == nil || err.Error() != "file_too_large" {
		t.Fatalf("big: %v", err)
	}
	for _, p := range []string{filepath.Join(dir, "nope"), dir} {
		if _, err := editCall(t, u, p, edit("a", "b")); err == nil {
			t.Fatalf("%s should fail", p)
		}
	}
	if _, err := editCall(t, u, big, edit("", "b")); err == nil || err.Error() != "invalid_params" {
		t.Fatalf("empty old: %v", err)
	}
}

func TestUnifiedDiffJoinsNearbyChangesAndCapsItsLength(t *testing.T) {
	old := "a\nb\nc\nd\ne\nf\ng\nh\n"
	next := "a\nB\nc\nd\nE\nf\ng\nh\n"
	found := []replacement{{start: 2, end: 3, text: "B"}, {start: 8, end: 9, text: "E"}}
	diff, cut := unifiedDiff("f", old, next, found)
	want := "--- f\n+++ f\n@@ -1,8 +1,8 @@\n a\n-b\n+B\n c\n d\n-e\n+E\n f\n g\n h\n"
	if diff != want || cut {
		t.Fatalf("diff:\n%s", diff)
	}
	// A change inside one line, with no final newline.
	diff, _ = unifiedDiff("f", "x = 1", "x = 22", []replacement{{start: 4, end: 5, text: "22"}})
	if diff != "--- f\n+++ f\n@@ -1,1 +1,1 @@\n-x = 1\n+x = 22\n" {
		t.Fatalf("inline diff:\n%s", diff)
	}
	long := strings.Repeat("line\n", 5000)
	diff, cut = unifiedDiff("f", long, strings.ReplaceAll(long, "line", "LINE"), []replacement{{start: 0, end: len(long), text: strings.ReplaceAll(long, "line", "LINE")}})
	if !cut || len(diff) > diffLimit || !strings.HasSuffix(diff, "\n") {
		t.Fatalf("diff not capped: %d %v", len(diff), cut)
	}
}
