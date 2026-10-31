//go:build unix || windows

package node

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"
)

const (
	maxEdits = 100
	// More places than this for one edit are refused rather than held in memory.
	maxMatches = 100000
	diffLimit  = 12000
	// Lines of unchanged context around each change in the diff.
	diffContext = 3
)

type editParams struct {
	Path  string `json:"path"`
	Edits []struct {
		Old        string `json:"old"`
		New        string `json:"new"`
		ReplaceAll bool   `json:"replaceAll"`
	} `json:"edits"`
}

// editFailure names one edit that cannot be applied: not_found, multiple
// (Count places, the first at Lines), too_many or overlap (with edit With).
type editFailure struct {
	Edit   int    `json:"edit"`
	Reason string `json:"reason"`
	Count  int    `json:"count,omitempty"`
	Lines  []int  `json:"lines,omitempty"`
	With   int    `json:"with,omitempty"`
}

type replacement struct {
	start, end int
	text       string
	edit       int
}

// edit applies exact replacements to one UTF-8 text file. Every edit is
// matched against the same original content and nothing is written unless
// all of them apply; the new content replaces the file atomically with its
// owner and mode.
func (u *uploads) edit(raw json.RawMessage) (any, error) {
	var p editParams
	if json.Unmarshal(raw, &p) != nil || strings.ContainsRune(p.Path, 0) || len(p.Edits) == 0 || len(p.Edits) > maxEdits {
		return nil, errors.New("invalid_params")
	}
	for _, e := range p.Edits {
		if e.Old == "" {
			return nil, errors.New("invalid_params")
		}
	}
	path, ok := localPath(p.Path)
	if !ok {
		return nil, errors.New("invalid_params")
	}
	// Edits through this node never interleave; another program can still
	// write the file, which the check before the rename catches.
	u.editMu.Lock()
	defer u.editMu.Unlock()
	path, prior, err := target(path)
	if err != nil {
		return nil, err
	}
	if prior == nil {
		return nil, errors.New("not_found")
	}
	if !prior.Mode().IsRegular() && !reparseFile(prior) {
		return nil, errors.New("not_regular_file")
	}
	if prior.Size() > fileChunkMax {
		return nil, errors.New("file_too_large")
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fileError(err)
	}
	if len(data) > fileChunkMax {
		return nil, errors.New("file_too_large")
	}
	if bytes.IndexByte(data, 0) >= 0 || !utf8.Valid(data) {
		return nil, errors.New("not_text")
	}
	text := string(data)
	lines := lineStarts(text)
	crlf := strings.Contains(text, "\r\n")
	var found []replacement
	var failures []editFailure
	converted := false
	for i, e := range p.Edits {
		old, repl := e.Old, e.New
		at := occurrences(text, old, e.ReplaceAll)
		// Text typed with \n for a file written with \r\n.
		if len(at) == 0 && crlf && strings.Contains(old, "\n") {
			if alt := toCRLF(old); alt != old {
				if at = occurrences(text, alt, e.ReplaceAll); len(at) > 0 {
					old, repl, converted = alt, toCRLF(repl), true
				}
			}
		}
		switch {
		case len(at) > maxMatches:
			failures = append(failures, editFailure{Edit: i + 1, Reason: "too_many", Count: maxMatches})
		case len(at) == 0:
			failures = append(failures, editFailure{Edit: i + 1, Reason: "not_found"})
		case len(at) > 1 && !e.ReplaceAll:
			where := []int{}
			for _, pos := range at[:min(len(at), 5)] {
				where = append(where, lineOf(lines, pos))
			}
			failures = append(failures, editFailure{Edit: i + 1, Reason: "multiple", Count: len(at), Lines: where})
		default:
			for _, pos := range at {
				found = append(found, replacement{start: pos, end: pos + len(old), text: repl, edit: i + 1})
			}
		}
	}
	sort.Slice(found, func(i, j int) bool { return found[i].start < found[j].start })
	seen := map[[2]int]bool{}
	for k := 1; k < len(found); k++ {
		a, b := found[k-1], found[k]
		if b.start < a.end {
			pair := [2]int{min(a.edit, b.edit), max(a.edit, b.edit)}
			if !seen[pair] {
				seen[pair] = true
				failures = append(failures, editFailure{Edit: pair[1], Reason: "overlap", With: pair[0]})
			}
		}
	}
	shown := shownPath(path)
	if len(failures) > 0 {
		sort.SliceStable(failures, func(i, j int) bool { return failures[i].Edit < failures[j].Edit })
		return map[string]any{"path": shown, "changed": false, "failures": failures}, nil
	}
	var out strings.Builder
	last := 0
	for _, r := range found {
		out.WriteString(text[last:r.start])
		out.WriteString(r.text)
		last = r.end
	}
	out.WriteString(text[last:])
	next := out.String()
	if len(next) > fileChunkMax {
		return nil, errors.New("file_too_large")
	}
	result := map[string]any{"path": shown, "replacements": len(found), "size": len(next)}
	if converted {
		result["lineEndings"] = "crlf"
	}
	if next == text {
		result["changed"], result["diff"] = false, ""
		return result, nil
	}
	temp := filepath.Join(filepath.Dir(path), "."+filepath.Base(path)+".obtmp-"+randomHex(6))
	if err = writeFile(temp, []byte(next), os.O_WRONLY|os.O_CREATE|os.O_EXCL, true); err != nil {
		os.Remove(temp)
		return nil, err
	}
	if now, err := os.Stat(path); err != nil || now.Size() != prior.Size() || !now.ModTime().Equal(prior.ModTime()) {
		os.Remove(temp)
		return nil, errors.New("file_changed")
	}
	if err = commit(temp, path, "", prior); err != nil {
		return nil, err
	}
	diff, cut := unifiedDiff(shown, text, next, found)
	result["changed"], result["diff"], result["diffTruncated"] = true, diff, cut
	return result, nil
}

// occurrences finds old in text, up to one past maxMatches: every
// non-overlapping place for a replace all, else every start (overlapping
// ones too, so "aa" in "aaa" is two).
func occurrences(text, old string, all bool) []int {
	var at []int
	step := 1
	if all {
		step = len(old)
	}
	for from := 0; from <= len(text) && len(at) <= maxMatches; {
		i := strings.Index(text[from:], old)
		if i < 0 {
			break
		}
		at = append(at, from+i)
		from += i + step
	}
	return at
}

func toCRLF(s string) string {
	return strings.ReplaceAll(strings.ReplaceAll(s, "\r\n", "\n"), "\n", "\r\n")
}

func lineStarts(text string) []int {
	starts := []int{0}
	for i := 0; i < len(text); i++ {
		if text[i] == '\n' {
			starts = append(starts, i+1)
		}
	}
	return starts
}

// lineOf is the 1-based line holding byte pos.
func lineOf(starts []int, pos int) int {
	return sort.Search(len(starts), func(i int) bool { return starts[i] > pos })
}

func atLineStart(s string, p int) bool { return p == 0 || p == len(s) || s[p-1] == '\n' }

// lineCount is the number of lines in s[:p], counting an unterminated last one.
func lineCount(s string, p int) int {
	n := strings.Count(s[:p], "\n")
	if p == len(s) && p > 0 && s[p-1] != '\n' {
		n++
	}
	return n
}

// fileLines splits s into lines without their line endings.
func fileLines(s string) []string {
	lines := strings.SplitAfter(s, "\n")
	if lines[len(lines)-1] == "" {
		lines = lines[:len(lines)-1]
	}
	for i, l := range lines {
		lines[i] = strings.TrimSuffix(strings.TrimSuffix(l, "\n"), "\r")
	}
	return lines
}

// unifiedDiff describes the replacements as whole changed lines. Text
// outside the replacements is the same in both versions, so each change
// spans from the start of its first line to the first point after it where
// both versions are at a line boundary.
func unifiedDiff(name, old, next string, found []replacement) (string, bool) {
	type change struct{ a1, a2, b1, b2 int }
	var changes []change
	delta := 0
	for i := 0; i < len(found); {
		start := strings.LastIndexByte(old[:found[i].start], '\n') + 1
		before := delta
		var end int
		for {
			r := found[i]
			delta += len(r.text) - (r.end - r.start)
			end = r.end
			if !atLineStart(old, end) || !atLineStart(next, end+delta) {
				if j := strings.IndexByte(old[end:], '\n'); j >= 0 {
					end += j + 1
				} else {
					end = len(old)
				}
			}
			if i+1 < len(found) && strings.LastIndexByte(old[:found[i+1].start], '\n')+1 < end {
				i++
				continue
			}
			i++
			break
		}
		changes = append(changes, change{lineCount(old, start), lineCount(old, end), lineCount(next, start+before), lineCount(next, end+delta)})
	}
	oldLines, newLines := fileLines(old), fileLines(next)
	var out strings.Builder
	out.WriteString("--- " + name + "\n+++ " + name + "\n")
	for i := 0; i < len(changes); {
		j := i
		for j+1 < len(changes) && changes[j+1].a1-changes[j].a2 <= 2*diffContext {
			j++
		}
		from, to := max(0, changes[i].a1-diffContext), min(len(oldLines), changes[j].a2+diffContext)
		newFrom := from + changes[i].b1 - changes[i].a1
		newCount := to - from
		for _, c := range changes[i : j+1] {
			newCount += (c.b2 - c.b1) - (c.a2 - c.a1)
		}
		out.WriteString("@@ -" + hunkRange(from, to-from) + " +" + hunkRange(newFrom, newCount) + " @@\n")
		at := from
		for _, c := range changes[i : j+1] {
			for ; at < c.a1; at++ {
				out.WriteString(" " + oldLines[at] + "\n")
			}
			for _, l := range oldLines[c.a1:c.a2] {
				out.WriteString("-" + l + "\n")
			}
			for _, l := range newLines[c.b1:c.b2] {
				out.WriteString("+" + l + "\n")
			}
			at = c.a2
		}
		for ; at < to; at++ {
			out.WriteString(" " + oldLines[at] + "\n")
		}
		i = j + 1
	}
	diff := out.String()
	if len(diff) <= diffLimit {
		return diff, false
	}
	cut := strings.LastIndexByte(diff[:diffLimit], '\n') + 1
	return diff[:cut], true
}

func hunkRange(from, count int) string {
	if count == 0 {
		return strconv.Itoa(from) + ",0"
	}
	return strconv.Itoa(from+1) + "," + strconv.Itoa(count)
}
