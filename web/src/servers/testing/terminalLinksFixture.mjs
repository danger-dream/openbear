// The terminal output of terminalLinks.browser.test.mjs, shared with terminalLinks.dom.test.mjs.
// An OSC 8 link whose text names another host, then one whose text names none.
export const OSC = (uri, text) => `\x1b]8;;${uri}\x1b\\${text}\x1b]8;;\x1b\\`;
export const OUTPUT = `see ${OSC('https://evil.example/login?next=%2F', 'https://paypal.com')} or ${OSC('https://docs.example.com/guide', '使用说明')}\r\n$ `;
