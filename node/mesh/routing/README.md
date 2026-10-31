# Relay routing

Relays choose their next hop toward every other relay from link costs they
measure and share (contract, third batch, section 4). Package `routing` is
the logic, with an injectable clock; `health/routing.go` feeds it probes and
carries its advertisements; the engine turns its next hops into AllowedIPs.
Only relays route; members are unaffected.

## Link metrics

Every relay measures each relay it has a link with, dialed or punched, from
the health probes on that link, sent every `activeIntervalMs` (1 s).

| | |
|---|---|
| rtt | median of the last 5 answered probes' round trips |
| jitter | median of the last 5 distances of an answer from the rtt it made |
| loss | the lower of the lost shares of the last 10 and of the last 30 probes |
| down | 3 probes in a row lost, or the link's path gone (at once) |
| penalty | +1000 on every entry into down, capped at 4000, half-life 30 s |
| suppressed | penalty ≥ 2000; reused once below 750 |
| cost | `rtt + 2*jitter + 1000*loss + bias` ms, at least 0.1; infinite unless up |

- A new link is up after 3 answered probes; until then its rtt is the fastest of them, so a slow first answer
  while the path warms up does not weigh on it for half a minute.
- Medians, not averages: on a TCP link a lost segment holds the probes
  behind it for a retransmission timeout. With `ewma = 0.7*ewma + 0.3*rtt`
  and jitter averaged the same way, one such late answer raised a 159 ms
  link's cost to about 270 ms for longer than 5 s, and the relays' routes
  over their long TCP links went through a neighbor and back many times an
  hour (`TestLateAnswersDoNotFlap`, `TestRoutingNetnsTCPNoise`). Up to two
  late answers in any five now leave rtt and jitter as they were; a lasting
  change shows from its third probe, round trips that vary all the time
  still show as jitter.
- Who measures: the dialing end, and both ends of a punched link, time their
  own probes (lost after `max(1 s, min(maxTimeout, 3*srtt))`). The accepting
  end of a dialed link times its pong to the prober's `seen` (one round
  trip); a pong not seen in time, or a ping not arriving 1.5 intervals after
  the last, is a lost probe there. So both ends have a cost for every link.
- Loss: with the last 10 probes alone, two losses close together on a link
  that loses 3 % at random (+200 ms) sent a 180 ms link's routes over a
  255 ms detour and back about four times in ten minutes. Taking the lower
  share of the last 10 and 30 weighs such a pair by 30 probes (+67 ms) until
  losses persist; sustained loss raises both shares, and 10 good probes
  clear it, so sim.py's outage, flapping and degradation results are
  unchanged (`TestReplayMatchesSimulation`). Over 100 random ten-minute runs
  of that link the routes moved 0.13 times on average; 5 runs had two
  changes: five losses within 30 s, or three in a row (down by rule).
- `bias` is `routing.bias[<relay>]` from the config, -10000..10000 ms,
  default 0. The controller writes a link's preference into both ends.

## Advertised cost (damping)

A link's advertised cost moves only when the measured cost differs from it
by more than 10 ms **and** by the 0.8 factor either way (`new < 0.8*old` or
`old < 0.8*new`), continuously for 5 s. A change of state (up, down,
suppressed) is advertised at once. Routes are computed only from advertised
costs, this relay's own links included, and a link costs the larger of what
its two ends advertise. So every relay sees the same costs and the same
shortest paths, and short spikes move nothing.

## Advertisements

An advertisement is a relay's link state and next hops: origin, seq, its
links as (peer, cost, up | down | suppressed) and `r`, its next hop per
destination, used for the loop-free check. Two wire forms:

- **JSON**, in the control message's `advert` field, which every version
  reads: `{"o": origin, "s": seq, "l": [[peer, cost, "u"|"d"|"s"]...], "r":
  [[dest] | [dest, nextHop]...]}`. With 36-character ids one control
  message holds `r` only for about 6 relays and the links of about 15.
- **Compact**, in the field `ac`, read by this version and later: unpadded
  standard base64 of version byte 2, then unsigned varints: origin, seq,
  link count, per link (peer, state byte 0 down / 1 up / 2 suppressed, cost
  in tenths of a ms when up), a byte saying whether `r` follows, route
  count, per route (destination, next hop or 0 for the destination itself).
  Relays are named by their control address's host number (control address
  minus `network.controlPrefix`), the same in every relay's config. 32
  relays, each linked to all others, with `r` take 516 bytes; full
  advertisements fit up to 50 such relays in one control message.
  `TestCompactAdvertHolds32Relays` checks it. Without a distinct number for
  every relay a router sends and accepts JSON only.

Sending:
- Sent every 5 s, and on a state change or an advertised-cost change after a
  gap of at least 100 ms (a burst of changes goes out as one).
- To every linked relay in compact form, and in JSON as well to one that has
  sent no compact advertisement within 30 s: a relay of the version before
  ignores `ac` (it finds no `advert`) and still gets everything, each
  origin's advertisement re-encoded in JSON when forwarded to it. When a link
  comes up, its neighbor gets both again until it speaks compact, as it may
  have restarted as another version.
- `r` is left out of an advertisement that would not fit; receivers then
  assume the origin's shortest path. A relay of the version before receives
  JSON and so may lack `r` of large meshes, as before; mixed versions route
  the same as either alone (`TestRollingUpgrade`).
- `seq` is `max(last+1, unix µs)`, so a restarted relay's advertisements are
  newer than before. A receiver stores an advertisement with a higher seq
  than the one it holds, or the same seq bringing the `r` the held one left
  out, and forwards it to its other relay neighbors (not back, not to the
  origin); an older or repeated one is dropped silently. One not refreshed
  for 30 s is deleted. A forwarded advertisement keeps all of its `r`,
  the route to the forwarding relay included (before, that one was left
  out, and its neighbors took the origin for not routing to it:
  `TestForwardedAdvertKeepsRoutes`).
- When a link comes up, each end sends the other everything it holds, so the
  new neighbor need not wait for refreshes.
- Message type `route-advert` over the relay control channel (`health`):
  relays only, over a configured relay link, from a linked relay, on the
  authenticated control channel like every control message. Limits: one
  control message (1024 bytes) per advertisement, never fragmented; origin,
  every link peer and every route must be configured relays, no more entries
  than there are relays, each at most once, costs 0..10⁶; per neighbor 50 a
  second (burst 100) in all; per origin 20 new ones a second (burst 40).
  Rejections give one `routing.error` event a minute per neighbor.

## Choosing a next hop

Shortest paths by Dijkstra over the advertised links that neither end
reports down or suppressed; ties broken by a hash of each link's two ends, the same on every
relay. For each destination:

1. **Loop-free condition.** Neighbor N may be the next hop only if the path
   N advertises (following each relay's advertised next hop from N) reaches
   the destination without coming back here, over links that are up, at a
   cost strictly below this relay's cost through its current next hop
   (infinite when that route broke; see the convergence exception below). This also holds for the next hop in use:
   once its advertised path loops back or breaks, the route moves at once
   (reason `loop` or `down`).
2. **Damping.** While the route in use works, the shortest path's next hop is
   taken only when its path costs less than 0.8 times the current one and
   more than 10 ms less (reason `better`). The 5 s persistence comes from the
   advertised costs, which change only after 5 s.
3. **Fewer relays.** While the route in use works, a shortest path through
   strictly fewer relays (counted on the advertised paths) is taken when it
   costs less than 0.95 times the current one and at least 3 ms less, and
   that held for 5 s on end (reason `shorter`), so a link that comes back
   after a detour is used again: 77.5 ms direct replaces 83.2 ms through
   another relay, 74 ms through another relay does not replace 77.5 ms
   direct. Not while the first link's flap penalty is above 750, else a
   flapping link would be taken back every time it is up.
4. **Current path unusable.** Switch at once to the shortest path's next hop
   if it is loop-free, otherwise to the cheapest loop-free neighbor, or to
   none until advertisements catch up (reason `down`).
5. **Flap damping.** A route that goes back, by `better` or `shorter`, to
   the next hop it left by `better` or `shorter` less than 10 minutes
   before has flapped (not within 30 s of its first advertised route, while
   it converges). For 10 minutes after a flap, a move by `better` or
   `shorter` waits until its rule has held for 1 minute on end, doubled
   with each further flap in those 10 minutes, at most 8 minutes. Moves by
   `down` and `loop` never wait, nor does the move back to the next hop a
   route left by `down` once the link is usable again: an outage is still
   bypassed within seconds and its end used as before. Spikes shorter than
   the wait move a flapped route no more (`TestFlapDamping`).

These conditions use each relay's current view, not an atomic view of the
whole network. With converged advertisements, the next hops decrease cost
and are loop-free. During simultaneous failures, two neighboring relays
can lose their links to the same destination before receiving each other's
new advertisement. Rule 4 can then select each other using the old view,
forming a transient forwarding loop even while the destination is online
and reachable through another relay.

The loop clears when a fresh advertisement reaches either relay, directly
or by flooding over another path. With delivery, its duration includes the
control-path delay, advertisement scheduling and route-application ticks;
the simulation covers delayed delivery and loss on the direct link. These
are not a wall-clock upper bound under sustained advertisement loss or
shared-link congestion, whose load effects have not been measured here.
The packet TTL (64, `droppedTtl`) bounds each packet's forwarding, not the
loop's duration. Immediate failover is retained rather than delaying every
single-link failure while waiting for a new distributed view.

## Static mode and fallback

- `routing.mode` is `dynamic` (default) or `static`. Static uses
  `nextHops` and nothing else; the relay still measures and advertises.
- Until any advertisement has arrived, and again after all expired, a
  destination uses its configured `nextHops`. Once advertisements gave a
  destination a route, losing it means no route (never a stale static one).
- A next hop change rewrites AllowedIPs through the engine's reroute hook;
  sessions are kept. Nothing depends on the controller being online.

## Status and events

`status.routing`: `{mode, links: [{peer, state, rttMs, jitterMs, loss, cost,
penalty}], routes: [{dest, nextHop, cost, path, since, switches}]}`. `cost`
is `null` where infinite (link down or suppressed, no route); `path` is the
path as the relays on the way advertise it, `[]` without a route; `since` is
Unix ms; `switches` counts next hop changes after the first.

Events: `route.changed` (`<dest>: <old> -> <new> (<down|better|shorter|loop>)`, `none`
for no next hop), `link.suppressed` and `link.reused` (`<peer>: penalty N`),
`routing.error`.

## Tests

```sh
cd node && export HOME=/root GOTOOLCHAIN=local
go test -race ./mesh/routing                      # rules on a fixed clock, sim.py replay
go test -race -run 'TestRelayRouting|TestValidateRouting' ./mesh
OBLAB_NETNS=1 OBLAB_ROUTING=1 OBLAB_OLD_NODE=/tmp/old-node go test -run 'TestRoutingNetns' -v -timeout 20m ./mesh   # root
```

`TestRoutingNetnsTCPNoise` (`OBLAB_NOISE_MINUTES`, default 10) runs T, H, B
with B 159 and 175 ms away over TCP links losing 1 %: no route may move
more than twice, and a cut of T-B must still be bypassed within 5 s.
`TestReplayMatchesSimulation` feeds the per-second probes behind `sim.py`
(`testdata/sim-links.json`) to four routers. `TestRoutingNetnsFaults` runs
H, T, S, L as real data planes in `oblab-rt-*` namespaces with sim.py's round
trips and breaks H-T with netem (outage, flap, degrade), measuring the path
of pings H→T by round trip and TTL. `TestRoutingNetnsNoLoops` runs four
relays in a ring with a diagonal through cuts, flapping and partial
isolation with pings in both diagonals at an IP TTL of 6, and checks
`droppedTtl` stays 0. `TestRoutingNetnsRollingUpgrade` starts three relays as
the version before (`OBLAB_OLD_NODE`, e.g. a copy of `node/bin`'s
`openbear-node-linux-amd64` built before compact advertisements) and
restarts them one by one as this version while all ping each other, checking
that pings are lost only around a restart.
