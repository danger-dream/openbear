#!/usr/bin/env python3
"""Convert exported mesh_line_minute replay archive into a compact testdata fixture.

Reads workspace/artifacts/mesh-replay/mesh-replay-20261119-20261120.json.gz (or --input),
keeps relay-to-relay lines (kind=relay) whose both ends belong to the 25 relays in
revision 776, and writes a compact gzipped JSON to node/mesh/routing/testdata/replay-real.json.gz.
"""
from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT / "workspace/artifacts/mesh-replay/mesh-replay-20261119-20261120.json.gz"
DEFAULT_OUTPUT = ROOT / "node/mesh/routing/testdata/replay-real.json.gz"


def round_opt(v, digits=1):
    if v is None:
        return None
    return round(float(v), digits)


def build(src: Path, dst: Path) -> None:
    with gzip.open(src, "rt", encoding="utf-8") as f:
        data = json.load(f)

    revs = {r["revision"]: r for r in data["revisions"]}
    r776 = revs[776]
    relays = sorted(
        m["serverId"]
        for m in r776["members"]
        if m.get("role") == "relay" and not m.get("removing")
    )
    relay_set = set(relays)
    no_transit = sorted(
        m["serverId"]
        for m in r776["members"]
        if m.get("role") == "relay" and not m.get("removing") and m.get("noTransit")
    )
    names = {sid: (data.get("names") or {}).get(sid) or sid for sid in relays}

    # Build relay-to-relay lines whose both endpoints are in rev 776 relays.
    # Sort deterministically by (a, b).
    raw_lines = []
    for l in data["lines"]:
        if l.get("kind") != "relay":
            continue
        a, b = l["a"], l["b"]
        if a not in relay_set or b not in relay_set or a == b:
            continue
        if b < a:
            a, b = b, a
        raw_lines.append((a, b, l["id"]))
    raw_lines.sort()

    id_to_idx: dict[int, int] = {}
    lines_out = []
    for idx, (a, b, lid) in enumerate(raw_lines):
        id_to_idx[lid] = idx
        lines_out.append([a, b])

    cols = {name: i for i, name in enumerate(data["columns"])}
    i_line = cols["line"]
    i_ts = cols["ts"]
    i_rtt = cols["rtt"]
    i_min = cols["rtt_min"]
    i_max = cols["rtt_max"]
    i_loss = cols["loss"]
    i_up = cols["up_s"]
    i_known = cols["known_s"]

    # Collect rows per minute offset from start_ts.
    start_ts, end_ts = int(data["range"][0]), int(data["range"][1])
    total_minutes = (end_ts - start_ts) // 60 + 1

    # Compact per-line array of [minuteOffset, rtt10, min10, max10, loss10000, upS, knownS]
    # where rtt10 = round(rtt*10) or -1 if null, loss10000 = round(loss*10000) or 0 if null.
    per_line: list[list[list[int]]] = [[] for _ in range(len(lines_out))]
    kept_rows = 0
    for row in data["rows"]:
        idx = id_to_idx.get(row[i_line])
        if idx is None:
            continue
        m = (int(row[i_ts]) - start_ts) // 60
        if m < 0 or m >= total_minutes:
            continue
        rtt = row[i_rtt]
        rmin = row[i_min]
        rmax = row[i_max]
        loss = row[i_loss]
        up_s = int(row[i_up] or 0)
        known_s = int(row[i_known] or 0)
        rtt10 = -1 if rtt is None else max(1, int(round(float(rtt) * 10)))
        min10 = rtt10 if rmin is None else max(1, int(round(float(rmin) * 10)))
        max10 = rtt10 if rmax is None else max(min10, int(round(float(rmax) * 10)))
        if rtt10 >= 0:
            min10 = min(min10, rtt10)
            max10 = max(max10, rtt10)
        loss10k = 0 if loss is None else max(0, min(10000, int(round(float(loss) * 10000))))
        per_line[idx].append([m, rtt10, min10, max10, loss10k, up_s, known_s])
        kept_rows += 1

    for entries in per_line:
        entries.sort(key=lambda x: x[0])

    # Delta-encode minute offsets per line for much better gzip compression.
    # Format of each entry: [deltaMin, rtt10, min10, max10, loss10k, upS, knownS]
    encoded_lines = []
    for entries in per_line:
        prev = 0
        out = []
        for e in entries:
            dm = e[0] - prev
            prev = e[0]
            out.append([dm, e[1], e[2], e[3], e[4], e[5], e[6]])
        encoded_lines.append(out)

    out_obj = {
        "note": (
            "Compact relay-to-relay replay fixture built from mesh_line_minute (rev 776). "
            "series[lineIdx] entries are [deltaMin, rtt_x10, min_x10, max_x10, loss_x10000, up_s, known_s]; "
            "rtt_x10=-1 means no RTT sample in that minute."
        ),
        "startTs": start_ts,
        "minutes": total_minutes,
        "relays": relays,
        "noTransit": no_transit,
        "names": names,
        "lines": lines_out,
        "series": encoded_lines,
    }

    dst.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(out_obj, separators=(",", ":")).encode("utf-8")
    with gzip.open(dst, "wb", compresslevel=9) as f:
        f.write(raw)

    size = dst.stat().st_size
    print(
        f"wrote {dst}: {size:,} bytes ({size / (1024 * 1024):.2f} MiB), "
        f"relays={len(relays)}, noTransit={len(no_transit)}, "
        f"lines={len(lines_out)}, minutes={total_minutes}, rows={kept_rows:,}"
    )
    if size > 3 * 1024 * 1024:
        raise SystemExit(f"fixture exceeds 3 MiB limit: {size} bytes")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = p.parse_args()
    build(args.input, args.output)


if __name__ == "__main__":
    main()
