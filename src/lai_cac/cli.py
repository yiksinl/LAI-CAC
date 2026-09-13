from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from .assets import ReferenceAssets
from .composites import TARGET_HOURS_UTC, aligned_period
from .goes import decode_dqf, download, nearest_scan, sample_pixel
from .inference import run_estimate
from .serialization import strict_json_dumps

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
    estimate = sub.add_parser("estimate")
    estimate.add_argument("--start", type=date.fromisoformat, required=True)
    estimate.add_argument("--lat", type=float, required=True)
    estimate.add_argument("--lon", type=float, required=True)
    estimate.add_argument("--igbp-class", type=int, help="Auditable override; omit to use the exact bundled IGBP grid")
    estimate.add_argument("--location-name", help="Optional display label stored with the result provenance")
    estimate.add_argument("--output", type=Path, default=ROOT / "data/results/latest.json")
    args = parser.parse_args()

    if args.command == "audit":
        print(strict_json_dumps(
            ReferenceAssets(ROOT / "artifacts/reference").audit(
                validate_remote_navigation=True
            ),
            indent=2,
        ))
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
        print(strict_json_dumps(result, indent=2))
    elif args.command == "sample":
        scan = nearest_scan(args.date, args.hour)
        if scan is None:
            raise SystemExit("No matching scan found")
        target = ROOT / "data/cache" / Path(scan.key).name
        result = sample_pixel(download(scan, target), args.lat, args.lon)
        result["dqf_decoded"] = decode_dqf(int(result["dqf"]))
        print(strict_json_dumps(result, indent=2))
    else:
        result = run_estimate(
            args.lat, args.lon, args.start, args.igbp_class, ROOT, args.output, args.location_name
        )
        print(strict_json_dumps(
            {key: result[key] for key in ("status", "lai", "units", "model_sha256")},
            indent=2,
        ))


if __name__ == "__main__":
    main()
