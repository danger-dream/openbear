"""What a node's Go code accepts, checked on the controller. ddns.watch refuses the whole list
when one probe is bad, so a value the controller takes and the node refuses stops every probe
on that node. Ports of the parts of regexp/syntax (regexp.Compile, RE2) and net/url (url.Parse)
that decide acceptance; where Go releases differ, or a construct is not worth porting, the
controller refuses (\\p{Script} classes, brackets in a host other than one IPv6 literal)."""
from __future__ import annotations

import ipaddress

# unicode.Categories in Go 1.24 (the node's minimum), and Go's special "Any".
CATEGORIES = frozenset('Any C Cc Cf Co Cs L Ll Lm Lo Lt Lu M Mc Me Mn N Nd Nl No P Pc Pd Pe Pf Pi Po Ps S Sc Sk Sm So Z Zl Zp Zs'.split())
POSIX = frozenset(f'[:{s}{n}:]' for s in ('', '^') for n in
                  'alnum alpha ascii blank cntrl digit graph lower print punct space upper word xdigit'.split())
PERL_CLASSES = frozenset('dDsSwW')
C_ESCAPES = frozenset('afnrtv')
MAX_REPEAT = 1000


class _Bad(Exception):
    pass


def _alnum(c):
    return c.isascii() and c.isalnum()


def _hex(c):
    return c.isascii() and c in '0123456789abcdefABCDEF'


def regex_ok(pattern):
    """Whether Go's regexp.Compile (Perl flags) takes pattern."""
    if not isinstance(pattern, str):
        return False
    try:
        pattern.encode('utf-8')
        _Regex(pattern).parse()
    except (_Bad, UnicodeError):
        return False
    return True


class _Regex:
    """regexp/syntax's parser, keeping only what decides errors: the operand stack (an operand is
    needed for every repetition), stacked repetitions, {n,m} bounds and how deeply counted
    repetitions nest (repeatIsValid)."""

    def __init__(self, text):
        self.t = text
        self.i = 0
        self.frames = [[]]  # per open group: the operands since its '(' or the last '|'

    def peek(self, n=0):
        return self.t[self.i + n] if self.i + n < len(self.t) else ''

    def rest(self):
        return self.t[self.i:]

    def push(self, node):
        self.frames[-1].append(node)

    def parse(self):
        last_repeat = False
        while self.i < len(self.t):
            repeat = False
            c = self.t[self.i]
            if c == '(':
                if self.peek(1) == '?':
                    self.perl_flags()
                else:
                    self.i += 1
                    self.frames.append([])
            elif c == '|':
                self.i += 1
                self.frames[-1].append('|')
            elif c == ')':
                if len(self.frames) < 2:
                    raise _Bad
                self.i += 1
                inner = [n for n in self.frames.pop() if n != '|']
                self.push(('group', inner))
            elif c in '^$.':
                self.i += 1
                self.push(('atom',))
            elif c == '[':
                self.char_class()
                self.push(('atom',))
            elif c in '*+?':
                self.i += 1
                self.repeat(('op', c), last_repeat)
                repeat = True
            elif c == '{':
                bounds = self.counted()
                if bounds is None:
                    self.i += 1
                    self.push(('atom',))
                else:
                    low, high = bounds
                    if low < 0 or low > MAX_REPEAT or high > MAX_REPEAT or high >= 0 and low > high:
                        raise _Bad
                    self.repeat(('rep', low, high), last_repeat)
                    repeat = True
            elif c == '\\':
                self.backslash()
            else:
                self.i += 1
                self.push(('atom',))
            last_repeat = repeat
        if len(self.frames) != 1:
            raise _Bad

    def repeat(self, op, last_repeat):
        if self.peek() == '?':
            self.i += 1
        # Perl rejects stacked repetition (a**, a*+, a{2}{3}).
        if last_repeat:
            raise _Bad
        operands = self.frames[-1]
        if not operands or operands[-1] == '|':
            raise _Bad
        node = (*op, operands[-1])
        operands[-1] = node
        if op[0] == 'rep' and (op[1] >= 2 or op[2] >= 2) and not _repeat_valid(node, MAX_REPEAT):
            raise _Bad

    def counted(self):
        """(min, max) of a {n}, {n,} or {n,m} at the cursor, consumed; None (nothing consumed) when
        it is not one, which makes the { a literal. max is -1 for {n,}."""
        start = self.i
        self.i += 1
        low = self.integer()
        if low is None or not self.peek():
            self.i = start
            return None
        high = low
        if self.peek() == ',':
            self.i += 1
            if not self.peek():
                self.i = start
                return None
            if self.peek() == '}':
                high = -1
            else:
                high = self.integer()
                if high is None:
                    self.i = start
                    return None
                if high < 0:
                    low = -1
        if self.peek() != '}':
            self.i = start
            return None
        self.i += 1
        return low, high

    def integer(self):
        if not ('0' <= self.peek() <= '9'):
            return None
        if self.peek() == '0' and '0' <= self.peek(1) <= '9':
            return None
        n = 0
        while '0' <= self.peek() <= '9':
            if n >= 10 ** 8:
                n = -1
            elif n >= 0:
                n = n * 10 + int(self.peek())
            self.i += 1
        return n

    def perl_flags(self):
        t = self.rest()
        if len(t) > 4 and t[2] == 'P' and t[3] == '<' or len(t) > 3 and t[2] == '<':
            begin = 4 if t[2] == 'P' else 3
            end = t.find('>')
            if end < 0 or not t[begin:end] or not all(c == '_' or _alnum(c) for c in t[begin:end]):
                raise _Bad
            self.i += end + 1
            self.frames.append([])
            return
        self.i += 2
        sign, saw = 1, False
        while self.i < len(self.t):
            c = self.t[self.i]
            self.i += 1
            if c in 'imsU':
                saw = True
            elif c == '-':
                if sign < 0:
                    break
                sign, saw = -1, False
            elif c in ':)':
                if sign < 0 and not saw:
                    break
                if c == ':':
                    self.frames.append([])
                return
            else:
                break
        raise _Bad

    def backslash(self):
        nxt = self.peek(1)
        if nxt in ('A', 'b', 'B', 'z'):
            self.i += 2
            self.push(('atom',))
            return
        if nxt == 'C':
            raise _Bad
        if nxt == 'Q':
            end = self.t.find('\\E', self.i + 2)
            text = self.t[self.i + 2:] if end < 0 else self.t[self.i + 2:end]
            self.i = len(self.t) if end < 0 else end + 2
            for _ in text:
                self.push(('atom',))
            return
        if nxt in ('p', 'P'):
            self.unicode_class()
        elif nxt and nxt in PERL_CLASSES:
            self.i += 2
        else:
            self.escape()
        self.push(('atom',))

    def unicode_class(self):
        start = self.i
        self.i += 2
        if self.peek() == '{':
            end = self.t.find('}', start)
            if end < 0:
                raise _Bad
            name = self.t[start + 3:end]
            self.i = end + 1
        else:
            name = self.peek()
            self.i += 1
        if name.startswith('^'):
            name = name[1:]
        if name not in CATEGORIES:
            raise _Bad

    def escape(self):
        """One escaped character: Go's parseEscape."""
        self.i += 1
        if self.i >= len(self.t):
            raise _Bad
        c = self.t[self.i]
        self.i += 1
        if c.isascii() and not c.isalnum():
            return
        if c in '1234567' and '0' <= self.peek() <= '7' or c == '0':
            for _ in range(2):
                if '0' <= self.peek() <= '7':
                    self.i += 1
            return
        if c == 'x':
            if self.peek() == '{':
                self.i += 1
                digits, value = 0, 0
                while True:
                    d = self.peek()
                    if not d:
                        raise _Bad
                    self.i += 1
                    if d == '}':
                        break
                    if not _hex(d):
                        raise _Bad
                    value = value * 16 + int(d, 16)
                    if value > 0x10FFFF:
                        raise _Bad
                    digits += 1
                if not digits:
                    raise _Bad
                return
            x, y = self.peek(), self.peek(1)
            if not (x and _hex(x) and y and _hex(y)):
                raise _Bad
            self.i += 2
            return
        if c in C_ESCAPES:
            return
        raise _Bad

    def char_class(self):
        start = self.i
        self.i += 1
        if self.peek() == '^':
            self.i += 1
        first = True
        while self.i >= len(self.t) or self.t[self.i] != ']' or first:
            first = False
            t = self.rest()
            if len(t) > 2 and t.startswith('[:'):
                end = t.find(':]', 2)
                if end >= 0:
                    if t[:end + 2] not in POSIX:
                        raise _Bad
                    self.i += end + 2
                    continue
            if t[:1] == '\\' and t[1:2] in ('p', 'P'):
                self.unicode_class()
                continue
            if t[:1] == '\\' and t[1:2] and t[1:2] in PERL_CLASSES:
                self.i += 2
                continue
            low = self.class_char(start)
            if self.peek() == '-' and self.peek(1) not in ('', ']'):
                self.i += 1
                if self.class_char(start) < low:
                    raise _Bad
        self.i += 1

    def class_char(self, start):
        """The code point of one class character (an escape included), consumed."""
        if self.i >= len(self.t):
            raise _Bad
        if self.t[self.i] != '\\':
            self.i += 1
            return ord(self.t[self.i - 1])
        begin = self.i
        self.escape()
        return _escaped(self.t[begin:self.i])


def _escaped(text):
    """The code point an escape stands for (text is one that escape() took)."""
    c = text[1]
    if c == 'x':
        return int(text[3:-1] if text[2] == '{' else text[2:4], 16)
    if c in '01234567':
        return int(text[1:], 8)
    return {'a': 7, 'f': 12, 'n': 10, 'r': 13, 't': 9, 'v': 11}.get(c, ord(c))


def _repeat_valid(node, n):
    """Go's repeatIsValid: counted repetitions nested inside each other multiply to at most n."""
    kind = node[0]
    if kind == 'rep':
        m = node[2]
        if m == 0:
            return True
        if m < 0:
            m = node[1]
        if m > n:
            return False
        if m > 0:
            n //= m
        return _repeat_valid(node[3], n)
    if kind == 'op':
        return _repeat_valid(node[2], n)
    if kind == 'group':
        return all(_repeat_valid(sub, n) for sub in node[1])
    return True


# ----- net/url -----
HOST_OK = frozenset("-_.~!$&'()*+,;=:[]<>\"")
USERINFO_OK = frozenset("-._:~!$&'()*+,;=%@")


def _unescape_ok(text, mode=''):
    i = 0
    while i < len(text):
        c = text[i]
        if c == '%':
            if i + 2 >= len(text) or not _hex(text[i + 1]) or not _hex(text[i + 2]):
                return False
            if mode == 'host' and int(text[i + 1], 16) < 8 and text[i:i + 3] != '%25':
                return False
            if mode == 'zone':
                v = chr(int(text[i + 1:i + 3], 16))
                if text[i:i + 3] != '%25' and v != ' ' and not (_alnum(v) or v in HOST_OK):
                    return False
            i += 3
            continue
        if mode in ('host', 'zone') and c.isascii() and not (_alnum(c) or c in HOST_OK):
            return False
        i += 1
    return True


def _port_ok(text):
    return text == '' or text[0] == ':' and all('0' <= c <= '9' for c in text[1:])


def _host(host):
    """The host as url.Parse keeps it (still escaped), or None when it refuses it."""
    if host.startswith('['):
        end = host.rfind(']')
        if end < 0 or not _port_ok(host[end + 1:]) or '[' in host[1:end] or ']' in host[:end]:
            return None
        literal, _, zone = host[1:end].partition('%25')
        if '%' in literal:
            return None
        try:
            ipaddress.IPv6Address(literal)
        except ValueError:
            return None
        if '%25' in host[1:end] and not (zone and _unescape_ok(zone, 'zone')):
            return None
        return host
    if '[' in host or ']' in host:
        return None
    colon = host.rfind(':')
    if colon >= 0 and not _port_ok(host[colon:]):
        return None
    return host if _unescape_ok(host, 'host') else None


def _hostname(host):
    colon = host.rfind(':')
    if colon >= 0 and _port_ok(host[colon:]):
        host = host[:colon]
    if host.startswith('[') and host.endswith(']'):
        host = host[1:-1]
    return host


def url_ok(raw, limit=2048):
    """Whether the node takes raw as a probe URL: url.Parse succeeds, the scheme is http or https
    and there is a host name (the node's ddnsValidURL)."""
    if not isinstance(raw, str) or not raw:
        return False
    try:
        if len(raw.encode('utf-8')) > limit:
            return False
    except UnicodeError:
        return False
    if any(ord(c) < 0x20 or ord(c) == 0x7f or c == ' ' for c in raw):
        return False
    text, _, fragment = raw.partition('#')
    if not _unescape_ok(fragment):
        return False
    scheme, sep, rest = text.partition(':')
    if not sep or not scheme or not scheme[0].isascii() or not scheme[0].isalpha() \
            or not all(c.isascii() and (c.isalnum() or c in '+-.') for c in scheme) or scheme.lower() not in ('http', 'https'):
        return False
    if rest.endswith('?') and rest.count('?') == 1:
        rest = rest[:-1]
    else:
        rest = rest.partition('?')[0]
    if not rest.startswith('//'):
        return False
    authority, slash, path = rest[2:].partition('/')
    if not _unescape_ok(slash + path):
        return False
    userinfo, at, host = authority.rpartition('@')
    if at:
        if not all(_alnum(c) or c in USERINFO_OK for c in userinfo):
            return False
        if not all(_unescape_ok(part) for part in userinfo.split(':', 1)):
            return False
    host = _host(host)
    return host is not None and _hostname(host) != ''
