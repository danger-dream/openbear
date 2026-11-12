import test from 'node:test';
import assert from 'node:assert/strict';
import { elbow, pointAt, roundedPath, routeOrtho } from './meshRoute.js';

const box = (x, y, w, h) => ({x, y, w, h});
const segments = points => points.slice(1).map((p, i) => [points[i], p]);
// Whether a straight run passes through a box.
function through(a, b, r) {
  const x0 = Math.min(a.x, b.x), x1 = Math.max(a.x, b.x), y0 = Math.min(a.y, b.y), y1 = Math.max(a.y, b.y);
  return x1 > r.x && x0 < r.x + r.w && y1 > r.y && y0 < r.y + r.h;
}
// How long two lines run on top of each other.
function shared(p, q) {
  let s = 0;
  for (const [a, b] of segments(p)) for (const [c, d] of segments(q)) {
    if (a.y === b.y && c.y === d.y && a.y === c.y) s += Math.max(0, Math.min(Math.max(a.x, b.x), Math.max(c.x, d.x)) - Math.max(Math.min(a.x, b.x), Math.min(c.x, d.x)));
    if (a.x === b.x && c.x === d.x && a.x === c.x) s += Math.max(0, Math.min(Math.max(a.y, b.y), Math.max(c.y, d.y)) - Math.max(Math.min(a.y, b.y), Math.min(c.y, d.y)));
  }
  return s;
}

test('a line goes round the box in its way, at right angles, from border to border', () => {
  const boxes = new Map([['a', box(0, 200, 180, 60)], ['wall', box(260, 0, 200, 600)], ['b', box(540, 200, 180, 60)]]);
  const [route] = routeOrtho([{id: 'ab', source: 'a', target: 'b'}], boxes).values();
  for (const [p, q] of segments(route.points)) {
    assert.ok(p.x === q.x || p.y === q.y, 'straight across or up and down');
    assert.ok(!through(p, q, boxes.get('wall')), 'not through the wall');
  }
  const [first, last] = [route.points[0], route.points.at(-1)];
  // It leaves a's border and ends at b's (3 px off, as the curved lines do).
  assert.ok([183, -3].includes(first.x) || [197, 263].includes(first.y), JSON.stringify(first));
  assert.ok([537, 723].includes(last.x) || [197, 263].includes(last.y), JSON.stringify(last));
  assert.deepEqual([route.from, route.to], [boxes.get('a'), boxes.get('b')]);
  // The same board, the same lines.
  assert.deepEqual(routeOrtho([{id: 'ab', source: 'a', target: 'b'}], boxes), routeOrtho([{id: 'ab', source: 'a', target: 'b'}], boxes));
});

test('lines from one server share a trunk; lines between other servers keep lanes of their own', () => {
  // As drawn by hand: s below, a tall box beside it, three targets in a row above; lines from s run together.
  const boxes = new Map([['s', box(540, 500, 80, 60)], ['wall', box(300, 0, 200, 660)], ['t1', box(60, 100, 80, 60)], ['t2', box(650, 100, 100, 60)], ['t3', box(800, 100, 100, 60)]]);
  const lines = ['t1', 't2', 't3'].map(t => ({id: t, source: 's', target: t}));
  const r = routeOrtho(lines, boxes);
  assert.equal(r.size, 3);
  assert.ok(shared(r.get('t2').points, r.get('t3').points) > 0, 'two lines from s run together for a while');
  for (const route of r.values()) for (const [p, q] of segments(route.points)) assert.ok(!through(p, q, boxes.get('wall')));
  // Two pairs side by side: each line its own lane.
  const pairs = new Map([['a1', box(0, 0, 100, 40)], ['b1', box(600, 0, 100, 40)], ['a2', box(0, 60, 100, 40)], ['b2', box(600, 60, 100, 40)], ['mid', box(250, -40, 100, 180)]]);
  const two = routeOrtho([{id: '1', source: 'a1', target: 'b1'}, {id: '2', source: 'a2', target: 'b2'}], pairs);
  assert.equal(two.size, 2);
  assert.equal(shared(two.get('1').points, two.get('2').points), 0);
});

test('no way, no line; the shapes the board draws', () => {
  // b shut in on every side.
  const shut = new Map([['a', box(0, 0, 100, 40)], ['b', box(300, 300, 100, 40)], ['n', box(280, 260, 140, 30)], ['s', box(280, 350, 140, 30)], ['w', box(250, 260, 30, 120)], ['e', box(420, 260, 30, 120)]]);
  assert.equal(routeOrtho([{id: 'x', source: 'a', target: 'b'}], shut).size, 0);
  assert.equal(routeOrtho([{id: 'x', source: 'a', target: 'gone'}], shut).size, 0);
  // While a box is dragged: out of the facing side, across halfway, in.
  assert.deepEqual(elbow({cx: 50, cy: 20, w: 100, h: 40}, {cx: 400, cy: 120, w: 100, h: 40}), [{x: 103, y: 20}, {x: 225, y: 20}, {x: 225, y: 120}, {x: 347, y: 120}]);
  assert.deepEqual(pointAt([{x: 0, y: 0}, {x: 100, y: 0}, {x: 100, y: 100}], 0.75), {x: 100, y: 50});
  assert.equal(roundedPath([{x: 0, y: 0}, {x: 100, y: 0}, {x: 100, y: 100}], 6), 'M 0 0 L 94 0 Q 100 0 100 6 L 100 100');
});
