package browser

// Small ports of Python standard-library behaviour the engine depends on:
// mimetypes.guess_type, fnmatch.fnmatchcase, str.strip, pathlib str().

import (
	"bufio"
	"os"
	"regexp"
	"runtime"
	"strings"
	"sync"
	"unicode"
)

// ------------------------------------------------------------- mimetypes

var (
	mimeOnce  sync.Once
	mimeTypes map[string]string
)

// pyKnownMimeFiles is mimetypes.knownfiles (read in order, later wins).
var pyKnownMimeFiles = []string{
	"/etc/mime.types",
	"/etc/httpd/mime.types",
	"/etc/httpd/conf/mime.types",
	"/etc/apache/mime.types",
	"/etc/apache2/mime.types",
	"/usr/local/etc/httpd/conf/mime.types",
	"/usr/local/lib/netscape/mime.types",
	"/usr/local/etc/httpd/conf/mime.types",
	"/usr/local/etc/mime.types",
}

func mimeTable() map[string]string {
	mimeOnce.Do(func() {
		mimeTypes = map[string]string{}
		for ext, typ := range pyTypesDefault {
			mimeTypes[ext] = typ
		}
		if runtime.GOOS == "windows" {
			return // Python would consult the registry; not replicated.
		}
		for _, file := range pyKnownMimeFiles {
			info, err := os.Stat(file)
			if err != nil || !info.Mode().IsRegular() {
				continue
			}
			f, err := os.Open(file)
			if err != nil {
				continue
			}
			scanner := bufio.NewScanner(f)
			scanner.Buffer(make([]byte, 64<<10), 1<<20)
			for scanner.Scan() {
				for _, line := range strings.Split(scanner.Text(), "\r") {
					words := strings.Fields(line)
					for i, w := range words {
						if strings.HasPrefix(w, "#") {
							words = words[:i]
							break
						}
					}
					if len(words) == 0 {
						continue
					}
					for _, suff := range words[1:] {
						mimeTypes["."+suff] = words[0]
					}
				}
			}
			f.Close()
		}
	})
	return mimeTypes
}

// pySplitext is posixpath.splitext.
func pySplitext(p string) (string, string) {
	sep := strings.LastIndex(p, "/")
	dot := strings.LastIndex(p, ".")
	if dot > sep {
		for i := sep + 1; i < dot; i++ {
			if p[i] != '.' {
				return p[:dot], p[dot:]
			}
		}
	}
	return p, ""
}

var usesParams = map[string]bool{"": true, "ftp": true, "hdl": true, "prospero": true, "http": true, "imap": true,
	"https": true, "shttp": true, "rtsp": true, "rtspu": true, "sip": true, "sips": true, "mms": true, "sftp": true, "tel": true}

// guessType is mimetypes.guess_type(name)[0] for a file name, or "".
func guessType(name string) string {
	url := name
	scheme := ""
	if i := strings.Index(url, ":"); i > 0 && url[0] < 0x80 && unicode.IsLetter(rune(url[0])) {
		valid := true
		for _, c := range url[:i] {
			if !(c < 0x80 && (unicode.IsLetter(c) || unicode.IsDigit(c) || c == '+' || c == '-' || c == '.')) {
				valid = false
				break
			}
		}
		if valid {
			scheme = strings.ToLower(url[:i])
			url = url[i+1:]
		}
	}
	if len(scheme) > 1 {
		if strings.HasPrefix(url, "//") {
			rest := url[2:]
			end := strings.IndexAny(rest, "/?#")
			if end < 0 {
				end = len(rest)
			}
			url = rest[end:]
		}
		if i := strings.Index(url, "#"); i >= 0 {
			url = url[:i]
		}
		if i := strings.Index(url, "?"); i >= 0 {
			url = url[:i]
		}
		if usesParams[scheme] && strings.Contains(url, ";") {
			if slash := strings.LastIndex(url, "/"); slash >= 0 {
				if i := strings.Index(url[slash:], ";"); i >= 0 {
					url = url[:slash+i]
				}
			} else {
				url = url[:strings.Index(url, ";")]
			}
		}
		if scheme == "data" {
			comma := strings.Index(url, ",")
			if comma < 0 {
				return ""
			}
			typ := url[:comma]
			if semi := strings.Index(url[:comma], ";"); semi >= 0 {
				typ = url[:semi]
			}
			if strings.Contains(typ, "=") || !strings.Contains(typ, "/") {
				typ = "text/plain"
			}
			return typ
		}
	} else {
		url = name
	}
	base, ext := pySplitext(url)
	for {
		mapped, ok := pySuffixDefault[strings.ToLower(ext)]
		if !ok {
			break
		}
		base, ext = pySplitext(base + mapped)
	}
	if _, ok := pyEncodingsDefault[ext]; ok {
		_, ext = pySplitext(base)
	}
	return mimeTable()[strings.ToLower(ext)]
}

// ----------------------------------------------------------------- fnmatch

var fnCache sync.Map

// fnmatchCase is fnmatch.fnmatchcase(name, pattern).
func fnmatchCase(name, pattern string) (bool, error) {
	if re, ok := fnCache.Load(pattern); ok {
		return re.(*regexp.Regexp).MatchString(name), nil
	}
	re, err := regexp.Compile(fnTranslate(pattern))
	if err != nil {
		return false, err
	}
	fnCache.Store(pattern, re)
	return re.MatchString(name), nil
}

// fnTranslate ports fnmatch.translate (3.11) to an RE2 expression.
func fnTranslate(pattern string) string {
	pat := []rune(pattern)
	var res []string
	const star = "\x00STAR"
	i, n := 0, len(pat)
	for i < n {
		c := pat[i]
		i++
		switch c {
		case '*':
			if len(res) == 0 || res[len(res)-1] != star {
				res = append(res, star)
			}
		case '?':
			res = append(res, ".")
		case '[':
			j := i
			if j < n && pat[j] == '!' {
				j++
			}
			if j < n && pat[j] == ']' {
				j++
			}
			for j < n && pat[j] != ']' {
				j++
			}
			if j >= n {
				res = append(res, `\[`)
				continue
			}
			stuff := string(pat[i:j])
			// RE2 reads "[:" as a POSIX class, so a literal '[' is escaped too.
			escape := func(s string) string {
				return strings.ReplaceAll(strings.ReplaceAll(s, `\`, `\\`), "[", `\[`)
			}
			if !strings.Contains(stuff, "-") {
				stuff = escape(stuff)
			} else {
				var chunks []string
				k := i + 1
				if pat[i] == '!' {
					k = i + 2
				}
				for {
					idx := -1
					for q := k; q < j; q++ {
						if pat[q] == '-' {
							idx = q
							break
						}
					}
					if idx < 0 {
						break
					}
					chunks = append(chunks, string(pat[i:idx]))
					i = idx + 1
					k = idx + 3
				}
				chunk := string(pat[i:j])
				if chunk != "" {
					chunks = append(chunks, chunk)
				} else {
					chunks[len(chunks)-1] += "-"
				}
				for k := len(chunks) - 1; k > 0; k-- {
					prev, cur := []rune(chunks[k-1]), []rune(chunks[k])
					if len(prev) > 0 && len(cur) > 0 && prev[len(prev)-1] > cur[0] {
						chunks[k-1] = string(prev[:len(prev)-1]) + string(cur[1:])
						chunks = append(chunks[:k], chunks[k+1:]...)
					}
				}
				parts := make([]string, len(chunks))
				for q, s := range chunks {
					parts[q] = strings.ReplaceAll(escape(s), "-", `\-`)
				}
				stuff = strings.Join(parts, "-")
			}
			stuff = regexp.MustCompile(`([&~|])`).ReplaceAllString(stuff, `\$1`)
			i = j + 1
			switch {
			case stuff == "":
				res = append(res, `[^\x00-\x{10FFFF}]`)
			case stuff == "!":
				res = append(res, ".")
			default:
				if stuff[0] == '!' {
					stuff = "^" + stuff[1:]
				} else if stuff[0] == '^' {
					stuff = `\` + stuff
				}
				res = append(res, "["+stuff+"]")
			}
		default:
			res = append(res, regexp.QuoteMeta(string(c)))
		}
	}
	var out strings.Builder
	out.WriteString(`(?s)\A`)
	for _, piece := range res {
		if piece == star {
			out.WriteString(".*")
		} else {
			out.WriteString(piece)
		}
	}
	out.WriteString(`\z`)
	return out.String()
}

// ------------------------------------------------------------------ strings

// pyIsSpace is str.isspace for one code point.
func pyIsSpace(r rune) bool {
	switch r {
	case '\t', '\n', '\v', '\f', '\r', 0x1c, 0x1d, 0x1e, 0x1f, ' ', 0x85, 0xa0, 0x1680,
		0x2028, 0x2029, 0x202f, 0x205f, 0x3000:
		return true
	}
	return r >= 0x2000 && r <= 0x200a
}

func pyStrip(s string) string { return strings.TrimFunc(s, pyIsSpace) }

// pyWord is a Python 3 `\w` character.
func pyWord(r rune) bool { return r == '_' || unicode.IsLetter(r) || unicode.IsNumber(r) }

// pyPath is str(pathlib.Path(p)).
func pyPath(p string) string {
	if runtime.GOOS == "windows" {
		return pyWindowsPath(p)
	}
	if p == "" {
		return "."
	}
	prefix := ""
	if strings.HasPrefix(p, "/") {
		prefix = "/"
		if strings.HasPrefix(p, "//") && !strings.HasPrefix(p, "///") {
			prefix = "//"
		}
	}
	var parts []string
	for _, part := range strings.Split(p, "/") {
		if part != "" && part != "." {
			parts = append(parts, part)
		}
	}
	out := prefix + strings.Join(parts, "/")
	if out == "" {
		return "."
	}
	return out
}

func pyWindowsPath(p string) string {
	p = strings.ReplaceAll(p, "/", `\`)
	drive := ""
	if len(p) >= 2 && p[1] == ':' {
		drive, p = p[:2], p[2:]
	}
	root := ""
	if strings.HasPrefix(p, `\`) {
		root = `\`
	}
	var parts []string
	for _, part := range strings.Split(p, `\`) {
		if part != "" && part != "." {
			parts = append(parts, part)
		}
	}
	out := drive + root + strings.Join(parts, `\`)
	if out == "" {
		return "."
	}
	return out
}

// pyBaseName is pathlib.Path(p).name.
func pyBaseName(p string) string {
	normal := pyPath(p)
	sep := "/"
	if runtime.GOOS == "windows" {
		sep = `\`
	}
	if i := strings.LastIndex(normal, sep); i >= 0 {
		normal = normal[i+1:]
	}
	if normal == "." || (runtime.GOOS == "windows" && strings.HasSuffix(normal, ":")) {
		return ""
	}
	return normal
}
