from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from .assets import ReferenceAssets
from .composites import TARGET_HOURS_UTC, aligned_period
from .goes import decode_dqf, download, nearest_scan, sample_pixel

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser(prog="lai-cac")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("audit")
    scans = sub.add_parser("scans")
    scans.add_argument("--start", type=date.fromisoformat, required=True)
    sample = sub.add_parser("sample")
    sample.add_argument("--date", type=date.fromisoformat, required=True)
    sample.add_argument("--hour", type=int, choices=TARGET_HOURS_UTC, required=True)
    sample.add_argument("--lat", type=float, required=True)
    sample.add_argument("--lon", type=float, required=True)
    args = parser.parse_args()

    if args.command == "audit":
        print(json.dumps(ReferenceAssets(ROOT / "artifacts/reference").audit(), indent=2))
    elif args.command == "scans":
        start, end = aligned_period(args.start)
        result = []
        current = start
        while current <= end:
            for hour in TARGET_HOURS_UTC:
                scan = nearest_scan(current, hour)
                result.append({"date": current.isoformat(), "target_hour": hour,
                               "scan": scan.key if scan else None,
                               "started_at": scan.started_at.isoformat() if scan else None})
            current = date.fromordinal(current.toordinal() + 1)
        print(json.dumps(result, indent=2))
    else:
        scan = nearest_scan(args.date, args.hour)
        if scan is None:
            raise SystemExit("No matching scan found")
        target = ROOT / "data/cache" / Path(scan.key).name
        result = sample_pixel(download(scan, target), args.lat, args.lon)
        result["dqf_decoded"] = decode_dqf(int(result["dqf"]))
        print(json.dumps(result, indent=2, allow_nan=True))


if __name__ == "__main__":
    main()

