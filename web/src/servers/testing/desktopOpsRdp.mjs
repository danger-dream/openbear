// Test-only IronRDP boundary: real DesktopPane DOM handlers drive these sessions.
export const sessions = [];
export default async function init() {}
export function setup() {}
export class DesktopSize {
  constructor(width, height) { Object.assign(this, {width, height}); }
  free() {}
}
export class Extension {}
export class InputTransaction {
  events = [];
  addEvent(event) { this.events.push(event); }
}
export const DeviceEvent = Object.fromEntries(['mouseMove', 'mouseButtonPressed', 'mouseButtonReleased', 'keyPressed', 'keyReleased', 'wheelRotations', 'unicodePressed', 'unicodeReleased']
  .map(type => [type, (...args) => ({type, args})]));
export class SessionBuilder {
  username() {} password() {} serverDomain() {} destination() {} proxyAddress() {} authToken() {}
  desktopSize() {} renderCanvas() {} extension() {} setCursorStyleCallbackContext() {}
  setCursorStyleCallback() {} canvasResizedCallback() {} remoteClipboardChangedCallback() {} forceClipboardUpdateCallback() {}
  async connect() {
    let done;
    const run = new Promise(resolve => { done = resolve; });
    const session = {
      events: [], releases: 0,
      desktopSize: () => new DesktopSize(1280, 720),
      applyInputs(tx) { this.events.push(...tx.events); },
      releaseAllInputs() { this.releases++; },
      run: () => run,
      shutdown: () => done({reason: () => 'closed'}),
      free() {},
    };
    sessions.push(session);
    return session;
  }
}
