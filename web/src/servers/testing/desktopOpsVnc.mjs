export const sessions = [];
export default class RFB extends window.EventTarget {
  static messages = {pointerEvent(sock, x, y, mask) { sock.events.push({x, y, mask}); }};
  constructor(mount) {
    super();
    this._canvas = document.createElement('canvas');
    this._screen = mount;
    this._display = {};
    this._sock = {events: []};
    this._fbWidth = 1280;
    this._fbHeight = 720;
    mount.append(this._canvas);
    sessions.push(this);
    queueMicrotask(() => this.dispatchEvent(new window.CustomEvent('connect')));
  }
  disconnect() {}
  sendKey() {}
}
