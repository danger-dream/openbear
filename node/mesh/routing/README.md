# Relay routing

Relays choose their next hop toward every other relay from link costs they
measure and share. Package `routing` holds the logic on an injectable clock;
`health/routing.go` feeds it probes and carries its advertisements; the engine
turns its next hops into AllowedIPs. Both relays and routed members (leaves)
use it.

## Parameters

`routing.tuning` in the config sets them (`Tuning`, the controller's
`settings.routing`); without it, the defaults. Nodes advertising feature
`mesh.routing.smooth` receive the smoothed tuning; older N-1 nodes keep legacy
tuning while mixed versions run together.

| | default | range | |
|---|---|---|---|
| `smoothHalfLifeS` | 60 | 1..300 | half-life (s) of per-neighbor smoothed path cost |
| `switchPct` | 5 | 0..50 | cheaper by at least this % in both real and smoothed cost to move a working route |
| `switchMinMs` | 2 | 0..100 | and at least this many ms cheaper |
| `hopCostMs` | 2 | 0..50 | fixed ms added per up link so equal-cost paths prefer fewer hops |
| `startPenaltyMs` | 150 | 0..5000 | initial smoothed-cost penalty when a neighbor becomes usable, times 1 + the link's flap penalty / 1000 |
| `jitterWeight` | 2 | 0..10 | ms of cost per ms of jitter |
| `lossWeight` | 1000 | 0..10000 | ms of cost at 100% loss |

Legacy tuning payloads (pre-smoothing keys such as `switchMs`, `shorterMs`,
`advertMs`, `holdS`, `flapHoldS`, `flapHoldMaxS`) are ignored silently when
read and cleared on the next save.

## Link metrics

Every relay measures each link it carries from the health probes sent every
`activeIntervalMs` (1 s).

| | |
|---|---|
| `rtt` | median of the last 5 answered probes' round trips |
| `jitter` | median of the last 5 distances of an answer from the rtt it made |
| `loss` | `max(longLoss, burstLoss, slowLoss)` where `longLoss = max(0, lost100 - 1) / 100` (100-probe window ignoring a single stray loss), `burstLoss = lost10 / 10` when `lost10 >= 3` else `0` (fast reaction to real degradation), and `slowLoss` every lost probe averaged with a 15-minute half-life, an outage's probes and time left out (memory of a link that loses packets now and then) |
| `down` | 3 probes in a row lost, or the link's path gone (at once) |
| `penalty` | +1000 on every entry into down, capped at 4000, half-life 30 s |
| `suppressed` | penalty ≥ 2000; reused once below 750 |
| `kept` | a suppressed link still in use while no other way to a member works: the member keeps its cheapest; of the relays whose link to it is suppressed, the first by name keeps its own while no relay advertises one up |
| `cost` | `rtt + jitterWeight*jitter + lossWeight*loss + bias + narrow + hopCostMs` ms, at least 0.1; infinite unless up |

- A new link is up after 3 answered probes; until then its rtt is the fastest
  of them, so a slow first answer while the path warms up does not weigh on it.
- Medians, not averages: on a TCP link a lost segment holds probes behind it
  for a retransmission timeout. Up to two late answers in any five leave rtt
  and jitter unchanged; sustained change shows from the third probe, and steady
  jitter still shows as jitter.
- Windowed loss: 3% random loss averages ~2% penalty with minimal variance;
  a real 20–30% degradation triggers the 10-probe burst window within
  seconds.
- Slow loss: a single stray loss costs under 1 ms and halves every 15
  minutes, below the switch margin; a link losing probes in runs a minute or
  two apart, which the windows forget between runs, keeps its average rate
  (0.8% ≈ 8 ms) through its clean spells instead of drawing routes back in
  each. When a link goes down, the run of losses that made it down leaves
  slowLoss, and the outage's probes and time count for nothing.
- The probes a link lost while down (the run before its first answer) leave
  both windows when it answers again: an outage weighs on the link through
  its flap `penalty` and the start penalty below, not as loss. Counted as
  loss, a 20 s outage held the link at about +1000 ms for 10 s and +190 ms for
  100 s after it came back, and routes returned after 3.5 minutes
  (`TestSmoothFailoverRecoveryReturn`).
- `bias` is `routing.bias[<peer>]` from the config, -10000..10000 ms, default 0.
- `narrow` is 50 ms while a UDP link carries less than the network MTU from
  this end (`../linkmtu.go`, `SetNarrow`), else 0.
- There is no per-link `probation` / `held` state: flapping links are kept from
  attracting traffic by `penalty`/`suppressed` and by the start penalty, which
  grows with the link's `penalty` (`TestSmoothFlappingLinkAttractsNoTraffic`:
  20 s up and 20 s down, and 60 s up and 20 s down, a path 80 ms shorter).

## Advertisements

An advertisement is a node's link state and next hops: origin, seq, its links
as `(peer, cost, up | down | suppressed)` and `r`, its next hop per destination
used for the loop-free check, members (leaves) included. A held route is not
always its node's shortest path, so the others follow the next hops it gives;
one that gives none to a member (a node before 1.4.1, or left out to fit) is
taken to use its shortest path to it. Before 1.4.1 every relay was taken so for
members, and relays restarted one after the other looped traffic to one for
minutes (`TestMemberRoutesRollingRestart`). Over the 1024-byte budget the next
hops to members are left out first, then every next hop. Every data plane
with members in routing (feature `mesh.routing.members`, which the controller
waits for on every relay) reads next hops to members.

Advertised cost is the link's measured cost directly (no per-link hold timer).
Routes take the larger of what a link's two ends advertise.

Sending:
- Periodic refresh every 5 s.
- State change (`up` / `down` / `suppressed`) or next-hop change: coalesced for
  100 ms and sent immediately so failover and loop-free convergence stay fast.
- Pure cost change (no state or route change): advertised when
  `|measured - lastAdvertised| >= max(2% * lastAdvertised, 2 ms)` and at least
  2 s have elapsed since the previous pure-cost advertisement from this node.
- Two wire forms (**JSON** `advert` and **Compact** `ac`, version byte 2) remain
  unchanged for rolling-upgrade compatibility (`TestRollingUpgrade`).
- `seq` is `max(last+1, unix µs)`. Entries not refreshed for 30 s expire.
- Limits: one 1024-byte control message per advertisement; per origin 20 new
  ones/s (burst 40); per neighbor 50 invalid ones/s (burst 100), those that
  do not decode or verify, past which its messages are dropped unread until
  the bucket refills. Sound advertisements, forwarded or repeated, cost a
  neighbor nothing: when a link goes down every relay advertises at once and
  each neighbor forwards them all, which a limit on every message cut off
  just when routes needed them. `Status().Drops` counts what was turned away,
  by why (`advertDrops` in status.json).

## Choosing a next hop (smoothed path cost)

For each destination `D`, the router evaluates every direct up neighbor `N`
that can reach `D` (by Dijkstra over the advertised graph from `N` without
passing back through self). Each neighbor `N` gives a real path cost
`M(D, N) = linkCost(self, N) + dist(N, D)` and a toll count (number of
`noTransit` relays transited).

1. **Loop-free condition.** Neighbor `N` is loop-free if following advertised
   next hops from `N` reaches `D` over up links without revisiting self and at
   a real cost strictly below this router's cost through its current next hop
   (infinite if the current path broke). If the current next hop loops back or
   breaks, the route moves immediately (`loop` or `down`).
2. **Per-neighbor smoothed cost `S(D, N)`.** For every usable neighbor `N`,
   the router maintains an exponential moving average of `M(D, N)` updated on
   each tick (`dt`) with half-life `smoothHalfLifeS`:
   `S(D, N) += (M(D, N) - S(D, N)) * (1 - 2^(-dt / smoothHalfLifeS))`.
   - When `N` first becomes usable for `D` (initial appearance or recovery
     after being unreachable), `S(D, N)` starts at
     `M(D, N) + startPenaltyMs * (1 + penalty / 1000)`, `penalty` the flap
     penalty of the link to `N`: a link back from one outage pays about 1.6
     times `startPenaltyMs` and is taken again within a minute or two when much
     the better (87 s for a path 40% cheaper after a 20 s outage); one going
     down again and again starts too high to draw routes before it goes.
   - `S(D, N)` never gets further above `M(D, N)` than it started: one
     started on a passing spike (a link's first probes, a round trip that just
     fell) does not hold a much cheaper path off for minutes. A recovered link
     keeps the pace above, the cap only meets a real cost that fell.
   - Each `S(D, N)` draws its rate when it starts, 0.75 to 1.25 times the
     half-life's, and keeps it: relays and routes seeing the same change do not
     move together (babeld randomises its smoothing for the same reason).
   - When `N` becomes unreachable, `S(D, N)` is deleted so re-appearance pays
     the start penalty again.
3. **Switching rule.** While the current next hop `cur` is up and loop-free:
   - A loop-free neighbor `N` with strictly fewer `noTransit` tolls replaces
     `cur` (`better`) once that has held for half a second (`transitWait`),
     checked again against the latest advertisements at every step.
   - Among loop-free neighbors with the same toll count, with
     `margin(x) = max(x * switchPct / 100, switchMinMs)`, `N` replaces `cur`
     (`better`) only when **both** real cost and smoothed cost clear it:
     `M(D, N) <= M(D, cur) - margin(M(D, cur))` **and**
     `S(D, N) <= S(D, cur) - margin(S(D, cur))`.
   - If multiple neighbors qualify, the one with the lowest `S(D, N)` (then
     `M(D, N)`, then deterministic tie-breaker) wins.
4. **Current path unusable** (and at the start, with no route yet). Switch
   immediately to the loop-free neighbor with the fewest tolls and the lowest
   real cost `M`, or to none until advertisements catch up (`down`). Smoothing
   does not delay this.

This replaces the earlier five separate mechanisms (advert hold, shorter-hop
rule, per-link probation, post-hoc flap damping, and fixed switch hysteresis)
with a single continuous smoothed-cost rule.

Transient loops during simultaneous failures still clear as soon as a fresh
advertisement arrives directly or via flooding; packet TTL (64, `droppedTtl`)
bounds forwarding while advertisements are in flight. Loop-freedom is checked
on what each relay has heard, so two neighbors moving within one
advertisement's flight of each other can still point at each other for that
long; the half second a move off no-transit relays waits keeps the case that
caused such loops in the 11-21 replay, a path through one coming back to life
and the relays on it all leaving it at once, from happening at all.

## No-transit relays

`routing.noTransit` lists relays that forward between two other relays only
when no other path exists. Paths are ordered first by toll count, then by cost.
A loop-free path with fewer tolls is taken (`better`) regardless of cost or
smoothing, half a second after it first could be while the route in use still
works (`held: "settling"` meanwhile), at once when that route breaks; paths with
more tolls are never preferred while a lower-toll route works
(`held: "transit"`).

## Static mode and fallback

- `routing.mode` is `dynamic` (default) or `static`. Static uses `nextHops` and
  nothing else; the relay still measures and advertises.
- Until any advertisement arrives, and after all expire, a destination uses
  configured `nextHops`. Once dynamic routes exist, losing all paths yields no
  route rather than a stale static one.

## Status and events

`status.routing`: `{mode, links: [{peer, state, rttMs, jitterMs, loss, cost,
penalty, advertised}], routes: [{dest, nextHop, cost, smoothed?, path, since,
switches, flips, best?, bestCost?, bestSmoothed?, held?}]}`.

- `cost` is the current route's real advertised cost and `smoothed` its
  smoothed cost `S(D, cur)` (`null` / omitted when infinite).
- `switches` counts next-hop changes after the first; `flips` counts voluntary
  (`better`) moves back to a next hop left voluntarily within the last 5
  minutes.
- While a route stays on `cur` though another neighbor has a lower real cost
  (or fewer tolls), `best` is that neighbor, `bestCost` its real cost,
  `bestSmoothed` its smoothed cost, and `held` is one of:
  - `margin`: the real costs, or the smoothed ones near them, differ by less
    than the margin; nothing moves while the costs stay as they are
  - `settling`: the real cost clears the margin and a smoothed cost is still
    on its way, more than a margin from its real cost (a start penalty, or a
    change the smoothing is catching up with); or a move off `noTransit`
    relays waits its half second
  - `loop`: not loop-free yet
  - `transit`: crosses more `noTransit` relays

Events: `route.changed` (`<dest>: <old> -> <new> (<down|better|loop>)`, `none`
for no next hop), `link.suppressed` and `link.reused` (`<peer>: penalty N`),
`routing.error`.

## Tests

```sh
cd node && export HOME=/root GOTOOLCHAIN=local
go test -race ./mesh/routing
go test -race -run 'TestRelayRouting|TestValidateRouting' ./mesh
OBLAB_REAL_REPLAY=1 go test -v -run TestRealDataReplay -timeout 15m ./mesh/routing
OBLAB_NETNS=1 OBLAB_ROUTING=1 OBLAB_OLD_NODE=/tmp/old-node go test -run 'TestRoutingNetns' -v -timeout 20m ./mesh   # root
```

- `TestRealDataReplay` replays real recorded relay link metrics from
  `testdata/replay-real.json.gz` (5 min smoke by default; `OBLAB_REAL_REPLAY=1`
  for 6 h slice; `OBLAB_REPLAY=1` for the full 48.3 h dataset) comparing
  `legacy` against smoothed routing across `smoothHalfLifeS ∈ {8, 15, 30, 60}s`,
  outputting a Markdown report and JSON summary to
  `workspace/artifacts/mesh-replay/`.
- `TestRoutingNetnsFaults`, `TestRoutingNetnsNoLoops`,
  `TestRoutingNetnsTCPNoise` and `TestRoutingNetnsRollingUpgrade` run real data
  planes in Linux network namespaces with `tc netem`.
