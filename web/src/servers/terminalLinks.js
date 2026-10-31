// OSC 8 links in the terminal: the program shows any text and links it to any address.
// What the page tells about a link before it is opened comes from here.

// Control characters and bidirectional marks, which could make shown text read as something else.
const HIDDEN = /[\u0000-\u001f\u007f-\u009f\u061c\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]/g;
// The first host-like thing in shown text: a domain (dots as browsers read them in a host, so
// "paypal。com" counts) or an IPv4 address.
const HOST = /(?:[\p{L}\p{N}](?:[\p{L}\p{N}-]*[\p{L}\p{N}])?[.\u3002\uff0e\uff61])+\p{L}[\p{L}\p{N}-]*|\d{1,3}(?:\.\d{1,3}){3}/u;
export const SHOWN_LIMIT = 200;
export const ADDRESS_LIMIT = 2000;

export function plain(text, limit) {
  const clean = String(text ?? '').replace(HIDDEN, '').trim();
  return clean.length > limit ? clean.slice(0, limit) + '…' : clean;
}

// The host shown text names, as the browser would write it (IDN as punycode), or '' when it names none.
export function shownHost(text) {
  const hit = String(text ?? '').replace(HIDDEN, '').match(HOST);
  if (!hit) return '';
  try { return new URL(`http://${hit[0]}`).hostname; } catch { return ''; }
}

const bare = host => host.replace(/\.$/, '').replace(/^www\./, '');

// {url, href, host, shown, shownHost, mismatch}: the address really opened (url; '' when it is not one) and how it
// is written for reading (punycode host, escaped path), the text the terminal shows for it (when that is not the
// address itself), and whether that text names another host than the one opened.
export function linkInfo(text, uri) {
  let url = '', host = '';
  try {
    const parsed = new URL(uri);
    if (['http:', 'https:'].includes(parsed.protocol)) { url = parsed.href; host = parsed.hostname; }
  } catch { /* not a web address: never opened */ }
  const href = plain(url || uri, ADDRESS_LIMIT), shown = plain(text, SHOWN_LIMIT), named = shownHost(text);
  const same = shown.replace(/\/$/, '') === href.replace(/\/$/, '');
  return {url, href, host, shown: same ? '' : shown, shownHost: named, mismatch: Boolean(named) && bare(named) !== bare(host)};
}

// The readable address cut around its host, [before, host, after], so the host can stand out; [href, '', ''] without one.
export function addressParts(info) {
  const at = info.host ? info.href.indexOf(info.host, info.href.indexOf('//') + 2) : -1;
  return at < 0 ? [info.href, '', ''] : [info.href.slice(0, at), info.host, info.href.slice(at + info.host.length)];
}
