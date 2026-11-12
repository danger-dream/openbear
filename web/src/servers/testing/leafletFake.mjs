// Leaflet for the happy-dom tests (IpMap.vue), which have no layout to draw a map in: what the map was told
// to show is kept on its element (data-center, data-zoom, data-bounds), the controls and the pin are plain
// elements, and its tiles load, or fail when globalThis.leafletTiles is 'fail', a moment after it is made.
// The browser tests use the real one.
const tileEvents = () => globalThis.leafletTiles === 'fail' ? ['tileerror', 'tileerror', 'tileerror'] : ['tileload'];

class Evented {
  constructor() { this.handlers = {}; }
  on(name, fn) { (this.handlers[name] ||= []).push(fn); return this; }
  fire(name) { for (const fn of this.handlers[name] || []) fn({type: name}); return this; }
}

class FakeMap extends Evented {
  constructor(el, options) {
    super();
    this.el = el;
    this.options = options;
    this.zoom = 0;
    el.classList.add('leaflet-container');
    el.dataset.dragging = String(options.dragging !== false);
    let wheel = options.scrollWheelZoom !== false;
    this.scrollWheelZoom = {enable: () => { wheel = true; }, disable: () => { wheel = false; }, enabled: () => wheel};
    this.click = () => this.fire('click');
    el.addEventListener('click', this.click);
  }
  setView([lat, lon], zoom) { this.el.dataset.center = `${lat.toFixed(2)},${lon.toFixed(2)}`; this.setZoom(zoom); delete this.el.dataset.bounds; return this; }
  fitBounds([[s, w], [n, e]]) { this.el.dataset.bounds = [s, w, n, e].map(v => v.toFixed(1)).join(','); delete this.el.dataset.center; return this; }
  setZoom(zoom) { this.zoom = zoom; this.el.dataset.zoom = String(zoom); return this; }
  invalidateSize() { return this; }
  remove() { this.el.removeEventListener('click', this.click); this.el.replaceChildren(); this.el.classList.remove('leaflet-container'); }
}

export const map = (el, options = {}) => new FakeMap(el, options);

export const control = {
  zoom: ({zoomInTitle, zoomOutTitle}) => ({addTo(m) {
    const bar = m.el.ownerDocument.createElement('div');
    bar.className = 'leaflet-control-zoom';
    for (const [title, text, step] of [[zoomInTitle, '+', 1], [zoomOutTitle, '−', -1]]) {
      const a = m.el.ownerDocument.createElement('a');
      Object.assign(a, {title, textContent: text, role: 'button'});
      a.addEventListener('click', event => { event.stopPropagation(); m.setZoom(m.zoom + step); });
      bar.append(a);
    }
    m.el.append(bar);
    return this;
  }}),
  attribution: () => ({addTo(m) {
    const box = m.el.ownerDocument.createElement('div');
    box.className = 'leaflet-control-attribution';
    m.el.append(box);
    m.attribution = box;
    return this;
  }}),
};

export function tileLayer(url, {attribution = ''} = {}) {
  const layer = new Evented();
  layer.addTo = m => {
    if (m.attribution) m.attribution.innerHTML = attribution;
    m.el.dataset.tiles = url;
    setTimeout(() => { for (const name of tileEvents()) layer.fire(name); }, 0);
    return layer;
  };
  return layer;
}

export const divIcon = options => options;

export const marker = ([lat, lon], {icon}) => ({addTo(m) {
  const pin = m.el.ownerDocument.createElement('div');
  pin.className = icon.className;
  pin.innerHTML = icon.html;
  pin.dataset.at = `${lat.toFixed(2)},${lon.toFixed(2)}`;
  m.el.append(pin);
  return this;
}});
