package browser

// Ordered JSON values with Python json semantics. CDP payloads, page values
// and worker replies keep object key order, integer/float distinction and
// the exact text that Python's json.dumps(ensure_ascii=False,
// separators=(",", ":")) produces, so artifacts and size limits match.

import (
	"bytes"
	"encoding/json"
	"errors"
	"math"
	"sort"
	"strconv"
	"strings"
	"unicode/utf16"
	"unicode/utf8"
)

// Obj is an insertion-ordered JSON object (a Python dict).
type Obj struct {
	keys []string
	vals map[string]any
}

// keyError mirrors Python's KeyError: an unexpected missing field. It is
// raised with panic and reported as browser_operation_failed.
type keyError string

func (k keyError) Error() string { return "missing key " + strconv.Quote(string(k)) }

func newObj() *Obj { return &Obj{vals: map[string]any{}} }

// O builds an ordered object from alternating key/value arguments.
func O(kv ...any) *Obj {
	o := newObj()
	for i := 0; i+1 < len(kv); i += 2 {
		o.Set(kv[i].(string), kv[i+1])
	}
	return o
}

func (o *Obj) Get(k string) (any, bool) {
	if o == nil {
		return nil, false
	}
	v, ok := o.vals[k]
	return v, ok
}

// Def is dict.get(k, default).
func (o *Obj) Def(k string, def any) any {
	if v, ok := o.Get(k); ok {
		return v
	}
	return def
}

func (o *Obj) Has(k string) bool { _, ok := o.Get(k); return ok }

// Must is dict[k]; a missing key panics like Python's KeyError.
func (o *Obj) Must(k string) any {
	v, ok := o.Get(k)
	if !ok {
		panic(keyError(k))
	}
	return v
}

func (o *Obj) Set(k string, v any) *Obj {
	if _, ok := o.vals[k]; !ok {
		o.keys = append(o.keys, k)
	}
	o.vals[k] = v
	return o
}

func (o *Obj) Del(k string) {
	if o == nil {
		return
	}
	if _, ok := o.vals[k]; !ok {
		return
	}
	delete(o.vals, k)
	for i, key := range o.keys {
		if key == k {
			o.keys = append(o.keys[:i:i], o.keys[i+1:]...)
			break
		}
	}
}

func (o *Obj) Keys() []string {
	if o == nil {
		return nil
	}
	return append([]string(nil), o.keys...)
}

func (o *Obj) Len() int {
	if o == nil {
		return 0
	}
	return len(o.keys)
}

// Copy is a shallow dict.copy().
func (o *Obj) Copy() *Obj {
	c := newObj()
	if o != nil {
		for _, k := range o.keys {
			c.Set(k, o.vals[k])
		}
	}
	return c
}

// Update is dict.update(other).
func (o *Obj) Update(other *Obj) {
	if other == nil {
		return
	}
	for _, k := range other.keys {
		o.Set(k, other.vals[k])
	}
}

// Str returns the string value of k or "".
func (o *Obj) Str(k string) string {
	v, _ := o.Get(k)
	s, _ := v.(string)
	return s
}

// Obj returns the object value of k or nil.
func (o *Obj) Obj(k string) *Obj {
	v, _ := o.Get(k)
	r, _ := v.(*Obj)
	return r
}

func (o *Obj) MarshalJSON() ([]byte, error) { return dumps(o) }

// mustObj/mustStr/... are Python subscripts on dynamic values: a wrong
// type behaves like a TypeError (browser_operation_failed).
type typeError string

func (t typeError) Error() string { return "type error: " + string(t) }

func asObj(v any) *Obj {
	o, ok := v.(*Obj)
	if !ok {
		panic(typeError("object expected"))
	}
	return o
}

func asStr(v any) string {
	s, ok := v.(string)
	if !ok {
		panic(typeError("string expected"))
	}
	return s
}

func asList(v any) []any {
	switch l := v.(type) {
	case []any:
		return l
	case nil:
		panic(typeError("list expected"))
	}
	panic(typeError("list expected"))
}

// num converts a JSON/Go number (or bool, like Python) to float64.
func num(v any) (float64, bool) {
	switch n := v.(type) {
	case json.Number:
		f, err := strconv.ParseFloat(string(n), 64)
		if err != nil {
			var ne *strconv.NumError
			if errors.As(err, &ne) && errors.Is(ne.Err, strconv.ErrRange) {
				return f, true
			}
			return 0, false
		}
		return f, true
	case float64:
		return n, true
	case int:
		return float64(n), true
	case int64:
		return float64(n), true
	case bool:
		if n {
			return 1, true
		}
		return 0, true
	}
	return 0, false
}

func mustNum(v any) float64 {
	f, ok := num(v)
	if !ok {
		panic(typeError("number expected"))
	}
	return f
}

// isInt reports whether v is an integral JSON number (Python int).
func isIntNumber(v any) bool {
	switch n := v.(type) {
	case json.Number:
		return !strings.ContainsAny(string(n), ".eE")
	case int, int64:
		return true
	}
	return false
}

// truthy is Python's bool(value).
func truthy(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case string:
		return x != ""
	case *Obj:
		return x.Len() > 0
	case []any:
		return len(x) > 0
	case []string:
		return len(x) > 0
	case map[string]any:
		return len(x) > 0
	}
	if f, ok := num(v); ok {
		return f != 0
	}
	return true
}

// pyEqual is Python == for JSON scalars (True == 1, 1 == 1.0).
func pyEqual(a, b any) bool {
	if fa, ok := num(a); ok {
		if fb, ok := num(b); ok {
			return fa == fb
		}
		return false
	}
	switch x := a.(type) {
	case nil:
		return b == nil
	case string:
		y, ok := b.(string)
		return ok && x == y
	}
	return false
}

// pyStr is Python str() for JSON scalars.
func pyStr(v any) string {
	switch x := v.(type) {
	case string:
		return x
	case nil:
		return "None"
	case bool:
		if x {
			return "True"
		}
		return "False"
	case json.Number, int, int64, float64:
		return numberText(x)
	}
	b, _ := dumps(v)
	return string(b)
}

// ---------------------------------------------------------------- parsing

var errJSON = errors.New("invalid json")

// parseJSON decodes JSON into *Obj / []any / string / json.Number / bool / nil.
func parseJSON(data []byte) (any, error) {
	p := &jparser{data: data}
	p.ws()
	v, err := p.value(0)
	if err != nil {
		return nil, err
	}
	p.ws()
	if p.pos != len(p.data) {
		return nil, errJSON
	}
	return v, nil
}

type jparser struct {
	data []byte
	pos  int
}

func (p *jparser) ws() {
	for p.pos < len(p.data) {
		switch p.data[p.pos] {
		case ' ', '\t', '\n', '\r':
			p.pos++
		default:
			return
		}
	}
}

func (p *jparser) value(depth int) (any, error) {
	if depth > 10000 || p.pos >= len(p.data) {
		return nil, errJSON
	}
	switch c := p.data[p.pos]; {
	case c == '{':
		p.pos++
		o := newObj()
		p.ws()
		if p.pos < len(p.data) && p.data[p.pos] == '}' {
			p.pos++
			return o, nil
		}
		for {
			p.ws()
			if p.pos >= len(p.data) || p.data[p.pos] != '"' {
				return nil, errJSON
			}
			k, err := p.str()
			if err != nil {
				return nil, err
			}
			p.ws()
			if p.pos >= len(p.data) || p.data[p.pos] != ':' {
				return nil, errJSON
			}
			p.pos++
			p.ws()
			v, err := p.value(depth + 1)
			if err != nil {
				return nil, err
			}
			o.Set(k, v)
			p.ws()
			if p.pos >= len(p.data) {
				return nil, errJSON
			}
			if p.data[p.pos] == ',' {
				p.pos++
				continue
			}
			if p.data[p.pos] == '}' {
				p.pos++
				return o, nil
			}
			return nil, errJSON
		}
	case c == '[':
		p.pos++
		l := []any{}
		p.ws()
		if p.pos < len(p.data) && p.data[p.pos] == ']' {
			p.pos++
			return l, nil
		}
		for {
			p.ws()
			v, err := p.value(depth + 1)
			if err != nil {
				return nil, err
			}
			l = append(l, v)
			p.ws()
			if p.pos >= len(p.data) {
				return nil, errJSON
			}
			if p.data[p.pos] == ',' {
				p.pos++
				continue
			}
			if p.data[p.pos] == ']' {
				p.pos++
				return l, nil
			}
			return nil, errJSON
		}
	case c == '"':
		return p.str()
	case c == 't':
		return p.lit("true", true)
	case c == 'f':
		return p.lit("false", false)
	case c == 'n':
		return p.lit("null", nil)
	case c == '-' || (c >= '0' && c <= '9'):
		return p.number()
	}
	return nil, errJSON
}

func (p *jparser) lit(word string, v any) (any, error) {
	if !bytes.HasPrefix(p.data[p.pos:], []byte(word)) {
		return nil, errJSON
	}
	p.pos += len(word)
	return v, nil
}

func (p *jparser) number() (any, error) {
	start := p.pos
	d := p.data
	if p.pos < len(d) && d[p.pos] == '-' {
		p.pos++
	}
	if p.pos >= len(d) {
		return nil, errJSON
	}
	if d[p.pos] == '0' {
		p.pos++
	} else if d[p.pos] >= '1' && d[p.pos] <= '9' {
		for p.pos < len(d) && d[p.pos] >= '0' && d[p.pos] <= '9' {
			p.pos++
		}
	} else {
		return nil, errJSON
	}
	if p.pos < len(d) && d[p.pos] == '.' {
		p.pos++
		n := p.pos
		for p.pos < len(d) && d[p.pos] >= '0' && d[p.pos] <= '9' {
			p.pos++
		}
		if p.pos == n {
			return nil, errJSON
		}
	}
	if p.pos < len(d) && (d[p.pos] == 'e' || d[p.pos] == 'E') {
		p.pos++
		if p.pos < len(d) && (d[p.pos] == '+' || d[p.pos] == '-') {
			p.pos++
		}
		n := p.pos
		for p.pos < len(d) && d[p.pos] >= '0' && d[p.pos] <= '9' {
			p.pos++
		}
		if p.pos == n {
			return nil, errJSON
		}
	}
	return json.Number(d[start:p.pos]), nil
}

func hex4(b []byte) (rune, bool) {
	if len(b) < 4 {
		return 0, false
	}
	var r rune
	for _, c := range b[:4] {
		r <<= 4
		switch {
		case c >= '0' && c <= '9':
			r |= rune(c - '0')
		case c >= 'a' && c <= 'f':
			r |= rune(c-'a') + 10
		case c >= 'A' && c <= 'F':
			r |= rune(c-'A') + 10
		default:
			return 0, false
		}
	}
	return r, true
}

func (p *jparser) str() (string, error) {
	p.pos++ // opening quote
	d := p.data
	start := p.pos
	// Fast path: no escapes.
	for p.pos < len(d) {
		c := d[p.pos]
		if c == '"' {
			s := d[start:p.pos]
			p.pos++
			if !utf8.Valid(s) {
				return "", errJSON
			}
			return string(s), nil
		}
		if c == '\\' || c < 0x20 {
			break
		}
		p.pos++
	}
	var b []byte
	b = append(b, d[start:p.pos]...)
	for p.pos < len(d) {
		c := d[p.pos]
		switch {
		case c == '"':
			p.pos++
			if !utf8.Valid(b) {
				return "", errJSON
			}
			return string(b), nil
		case c < 0x20:
			return "", errJSON
		case c == '\\':
			p.pos++
			if p.pos >= len(d) {
				return "", errJSON
			}
			e := d[p.pos]
			p.pos++
			switch e {
			case '"', '\\', '/':
				b = append(b, e)
			case 'b':
				b = append(b, '\b')
			case 'f':
				b = append(b, '\f')
			case 'n':
				b = append(b, '\n')
			case 'r':
				b = append(b, '\r')
			case 't':
				b = append(b, '\t')
			case 'u':
				r, ok := hex4(d[p.pos:])
				if !ok {
					return "", errJSON
				}
				p.pos += 4
				if utf16.IsSurrogate(r) {
					if r < 0xDC00 && p.pos+6 <= len(d) && d[p.pos] == '\\' && d[p.pos+1] == 'u' {
						if r2, ok := hex4(d[p.pos+2:]); ok && r2 >= 0xDC00 && r2 <= 0xDFFF {
							p.pos += 6
							r = utf16.DecodeRune(r, r2)
						} else {
							r = utf8.RuneError
						}
					} else {
						// Go strings cannot hold a lone surrogate.
						r = utf8.RuneError
					}
				}
				b = utf8.AppendRune(b, r)
			default:
				return "", errJSON
			}
		default:
			n := p.pos
			for p.pos < len(d) && d[p.pos] != '"' && d[p.pos] != '\\' && d[p.pos] >= 0x20 {
				p.pos++
			}
			b = append(b, d[n:p.pos]...)
		}
	}
	return "", errJSON
}

// ---------------------------------------------------------------- encoding

type unsupportedValue struct{}

func (unsupportedValue) Error() string { return "unsupported json value" }

// dumps is json.dumps(v, ensure_ascii=False, separators=(",", ":")).
func dumps(v any) ([]byte, error) {
	var b bytes.Buffer
	if err := dumpValue(&b, v, false); err != nil {
		return nil, err
	}
	return b.Bytes(), nil
}

// dumpsASCII is json.dumps(v) with the default ensure_ascii=True and
// default ", " / ": " separators.
func dumpsASCII(v any) string {
	var b bytes.Buffer
	if err := dumpValueSep(&b, v, true, ", ", ": "); err != nil {
		panic(err)
	}
	return b.String()
}

func dumpValue(b *bytes.Buffer, v any, ascii bool) error {
	return dumpValueSep(b, v, ascii, ",", ":")
}

func dumpValueSep(b *bytes.Buffer, v any, ascii bool, item, key string) error {
	switch x := v.(type) {
	case nil:
		b.WriteString("null")
	case bool:
		if x {
			b.WriteString("true")
		} else {
			b.WriteString("false")
		}
	case string:
		quote(b, x, ascii)
	case json.Number, int, int64, float64:
		b.WriteString(numberText(x))
	case *Obj:
		b.WriteByte('{')
		if x != nil {
			for i, k := range x.keys {
				if i > 0 {
					b.WriteString(item)
				}
				quote(b, k, ascii)
				b.WriteString(key)
				if err := dumpValueSep(b, x.vals[k], ascii, item, key); err != nil {
					return err
				}
			}
		}
		b.WriteByte('}')
	case map[string]any:
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		b.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				b.WriteString(item)
			}
			quote(b, k, ascii)
			b.WriteString(key)
			if err := dumpValueSep(b, x[k], ascii, item, key); err != nil {
				return err
			}
		}
		b.WriteByte('}')
	case map[string]string:
		m := make(map[string]any, len(x))
		for k, s := range x {
			m[k] = s
		}
		return dumpValueSep(b, m, ascii, item, key)
	case []any:
		b.WriteByte('[')
		for i, e := range x {
			if i > 0 {
				b.WriteString(item)
			}
			if err := dumpValueSep(b, e, ascii, item, key); err != nil {
				return err
			}
		}
		b.WriteByte(']')
	case []string:
		b.WriteByte('[')
		for i, e := range x {
			if i > 0 {
				b.WriteString(item)
			}
			quote(b, e, ascii)
		}
		b.WriteByte(']')
	case []*Obj:
		b.WriteByte('[')
		for i, e := range x {
			if i > 0 {
				b.WriteString(item)
			}
			if err := dumpValueSep(b, e, ascii, item, key); err != nil {
				return err
			}
		}
		b.WriteByte(']')
	case []map[string]any:
		b.WriteByte('[')
		for i, e := range x {
			if i > 0 {
				b.WriteString(item)
			}
			if err := dumpValueSep(b, e, ascii, item, key); err != nil {
				return err
			}
		}
		b.WriteByte(']')
	case json.RawMessage:
		parsed, err := parseJSON(x)
		if err != nil {
			return err
		}
		return dumpValueSep(b, parsed, ascii, item, key)
	default:
		return unsupportedValue{}
	}
	return nil
}

const hexDigits = "0123456789abcdef"

// quote writes a JSON string exactly like Python's json encoder.
func quote(b *bytes.Buffer, s string, ascii bool) {
	b.WriteByte('"')
	for _, r := range s {
		switch {
		case r == '"':
			b.WriteString(`\"`)
		case r == '\\':
			b.WriteString(`\\`)
		case r == '\n':
			b.WriteString(`\n`)
		case r == '\r':
			b.WriteString(`\r`)
		case r == '\t':
			b.WriteString(`\t`)
		case r == '\b':
			b.WriteString(`\b`)
		case r == '\f':
			b.WriteString(`\f`)
		case r < 0x20 || (ascii && r > 0x7e):
			if r > 0xFFFF {
				r1, r2 := utf16.EncodeRune(r)
				writeU(b, r1)
				writeU(b, r2)
			} else {
				writeU(b, r)
			}
		default:
			b.WriteRune(r)
		}
	}
	b.WriteByte('"')
}

func writeU(b *bytes.Buffer, r rune) {
	b.WriteString(`\u`)
	b.WriteByte(hexDigits[(r>>12)&0xF])
	b.WriteByte(hexDigits[(r>>8)&0xF])
	b.WriteByte(hexDigits[(r>>4)&0xF])
	b.WriteByte(hexDigits[r&0xF])
}

// numberText formats like Python: ints verbatim, floats with float.__repr__.
func numberText(v any) string {
	switch n := v.(type) {
	case int:
		return strconv.Itoa(n)
	case int64:
		return strconv.FormatInt(n, 10)
	case float64:
		return pyFloat(n)
	case json.Number:
		s := string(n)
		if !strings.ContainsAny(s, ".eE") {
			// Python int(): "-0" is 0.
			if strings.TrimLeft(s, "-0") == "" {
				return "0"
			}
			return s
		}
		f, _ := num(n)
		return pyFloat(f)
	}
	return "null"
}

// pyFloat is repr(float) as emitted by json.dumps.
func pyFloat(f float64) string {
	switch {
	case math.IsNaN(f):
		return "NaN"
	case math.IsInf(f, 1):
		return "Infinity"
	case math.IsInf(f, -1):
		return "-Infinity"
	}
	if f == 0 {
		if math.Signbit(f) {
			return "-0.0"
		}
		return "0.0"
	}
	e := strconv.FormatFloat(f, 'e', -1, 64)
	exp, _ := strconv.Atoi(e[strings.IndexByte(e, 'e')+1:])
	if exp >= -4 && exp < 16 {
		s := strconv.FormatFloat(f, 'f', -1, 64)
		if !strings.Contains(s, ".") {
			s += ".0"
		}
		return s
	}
	return e
}

// pyLen is Python len(str): code points.
func pyLen(s string) int { return utf8.RuneCountInString(s) }

// pyPrefix is s[:n] in code points.
func pyPrefix(s string, n int) string {
	if n <= 0 {
		return ""
	}
	i := 0
	for pos := range s {
		if i == n {
			return s[:pos]
		}
		i++
	}
	return s
}
