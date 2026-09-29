#!/usr/bin/env python3
"""Extract only suite names/counts/durations; never copy JUnit properties or captured logs."""
import argparse
import datetime as dt
import json
import pathlib
import xml.etree.ElementTree as et


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", default="target")
    parser.add_argument("--output", default="artifacts/local/verification.json")
    args = parser.parse_args()
    build = pathlib.Path(args.build)
    suites = []
    for kind, directory in (("unit", "surefire-reports"), ("integration", "failsafe-reports")):
        paths = sorted((build / directory).glob("TEST-*.xml"))
        for path in paths:
            suite = et.parse(path).getroot()
            name = suite.attrib["name"]
            # An explicit -Dtest=SomeIT test can leave old integration XML in surefire.
            if kind == "unit" and name.endswith("IT"):
                continue
            row = {"kind": kind, "suite": name, "seconds": float(suite.attrib["time"])}
            row.update({key: int(suite.attrib[key]) for key in ("tests", "failures", "errors", "skipped")})
            suites.append(row)
    if not suites or not any(row["kind"] == "integration" for row in suites):
        raise SystemExit("Missing complete Maven verify reports; no result produced.")
    result = {"recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "source": "JUnit XML counts from Maven verify; no environment properties or log text exported",
              "suites": suites,
              "totals": {key: sum(row[key] for row in suites) for key in ("tests", "failures", "errors", "skipped")}}
    output = pathlib.Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["totals"]))
    return int(bool(result["totals"]["failures"] or result["totals"]["errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
