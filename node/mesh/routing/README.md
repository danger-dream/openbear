# Relay routing

Relays choose their next hop toward every other relay from link costs they
measure and share (contract, third batch, section 4). Package `routing` is
the logic, with an injectable clock; `health/routing.go` feeds it probes and
carries its advertisements; the engine turns its next hops into AllowedIPs.
Only relays route; members are unaffected.

## Parameters

`routing.tuning` in the config sets them (`Tuning`, the controller's
settings.routing); without it, the defaults. The tests written for the
former fixed values (`legacyTuning`: 20 % and more than 10 ms to move, 20 %
and 10 ms to advertise, shorter 5 % and 3 ms) keep them.

| | default | |
|---|---|---|
| switchMs, switchPct | 5, 0 | a working route moves to one at least this cheaper (ms), and with a share more than this % cheaper |
| shorterMs, shorterPct | 3, 0 | the same for a path through fewer relays, held `holdS` |
| advertMs, advertPct | 2, 0 | a link's advertised cost follows the measured one once they differ by more than this (ms, % of the larger) |
| holdS | 5 | how long an advertised cost change and the shorter rule must hold |
| jitterWeight, lossWeight | 2, 1000 | ms of cost per ms of jitter, for a link losing every probe |
| flapHoldS, flapHoldMaxS | 60, 480 | flap damping's first wait and its cap |

`TestReplayWithDefaults`: with the defaults sim.py's scenarios come out as
before (no switch without a fault, outage and degradation away and back
once) but for the flapping link, which costs a second detour (4 switches, 6
s lost against 2 and 3); `TestLossNoiseWithDefaults`: 3 % random loss on a
link 75 ms shorter than the detour moves once away and back in ten minutes.

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
| held | a link between relays back from down: down for 30 s, twice as long for each further down within 10 minutes, at most 8 minutes; let up at once while its peer is reachable no other way |
| kept | a suppressed link still in use while no other way to a member works: the member keeps its cheapest; of the relays whose link to it is suppressed, the first by name keeps its own while no relay advertises one up |
| cost | `rtt + jitterWeight*jitter + lossWeight*loss + bias + narrow` ms, at least 0.1; infinite unless up |

- A new link is up after 3 answered probes; until then its rtt is the fastest of them, so a slow first answer
  while the path warms up does not weigh on it for half a minute.
- Held: RFCHost-JP2 (2026-11-16) answered again on its links for seconds at a
  time, drew the other relays' routes and went down again; the penalty let a
  link back within a minute, and every relay moved its routes to it and away
  again. A link held is advertised down and status shows `heldMs`, what is
  left of the hold (`link.held <peer>: back after N downs in 10m0s, held for
  D`). It is let up when the peer is reachable no other way (`link.released
  <peer>: not reachable another way`): a relay that restarted comes back on
  every link at once, and each neighbor has others. A link to a member, and
  a member's own links, have no hold: `kept` keeps a member on the mesh
  (`TestALinkBackFromDownIsHeldLongerEachTime`, `TestARouteTakesALinkBackOnlyAfterItsHold`,
  `TestNoProbationOnAMembersLinks`).
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
- `narrow` is 50 ms while the link carries less than the network MTU from
  this end (`../linkmtu.go`, `SetNarrow`), else 0. It is advertised at once;
  as routes take the larger of the two ends' costs, the link costs it both
  ways.

## Advertised cost (damping)

A link's advertised cost moves only when the measured cost differs from it
by more than `advertMs` (and, with `advertPct`, more than that share of the
larger), continuously for `holdS`. A change of state (up, down,
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
   taken only when its path costs at least `switchMs` less (and, with
   `switchPct`, more than that share less; reason `better`). The
   persistence comes from the advertised costs, which change only after
   `holdS`.
3. **Fewer relays.** While the route in use works, a shortest path through
   strictly fewer relays (counted on the advertised paths) is taken when it
   costs at least `shorterMs` less (and `shorterPct`), and that held for
   `holdS` on end (reason `shorter`), so a link that comes back after a
   detour is used again. Not while the first link's flap penalty is above
   750, else a flapping link would be taken back every time it is up.
4. **Current path unusable.** Switch at once to the shortest path's next hop
   if it is loop-free, otherwise to the cheapest loop-free neighbor, or to
   none until advertisements catch up (reason `down`).
5. **Flap damping.** A route that goes back, by `better` or `shorter`, to
   the next hop it left by `better` or `shorter` less than 10 minutes
   before has flapped (not within 30 s of its first advertised route, while
   it converges). For 10 minutes after a flap, a move by `better` or
   `shorter` waits until its rule has held for `flapHoldS` on end, doubled
   with each further flap in those 10 minutes, at most `flapHoldMaxS`. Moves by
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

## No-transit relays

`routing.noTransit` lists relays (a home or office router with little
upload) that forward between two other relays only when no other path is
left. A path's cost is ordered first by its tolls, the times it goes on
through such a relay from a relay to a relay, then by cost and tie-breaker.
No toll is paid where the path starts (the relay's own traffic, its subnets'
and its members'), where it ends, toward a member (the relays a member
enters by carry it as their own), or by a member at the relay it enters by.

- A route through fewer tolls is taken at once by `better`, whatever it
  costs and without flap damping: once the way around is up again, the
  relay is left. Between paths with as many tolls the rules above hold on
  their costs alone.
- Status `cost` stays the paths' cost in ms; tolls do not show in it.
- Older nodes ignore the list and may still route through such a relay; the
  loop-free condition keeps mixed versions from looping.

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
penalty, advertised, heldMs?}], routes: [{dest, nextHop, cost, path, since, switches,
best?, bestCost?, held?, until?}]}`. `cost` is the measured cost and
`advertised` the one routes go by, `null` where infinite (link down or
suppressed, no route); `path` is the path as the relays on the way advertise
it, `[]` without a route; `since` is Unix ms; `switches` counts next hop
changes after the first. While a route stays on its next hop and the
shortest path's is another, `best` is that one, `bestCost` its cost along
the next hops advertised, `held` why: `margin` (cheaper by less than the
threshold), `hold` (the rule is met, until `until`), `damped` (flap damping,
until `until`), `loop` (not loop-free yet), `penalty` (a shorter path over a
link that went down lately), `transit` (through more no-transit relays).

Events: `route.changed` (`<dest>: <old> -> <new> (<down|better|shorter|loop>)`, `none`
for no next hop), `link.suppressed` and `link.reused` (`<peer>: penalty N`),
`link.held` and `link.released` (see Link metrics), `routing.error`.

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
