#!/usr/bin/env python3
"""Two-instance, synthetic-only HTTP experiment. Credentials and bodies never enter reports."""
import argparse
import concurrent.futures
import datetime as dt
import json
import pathlib
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid


class Experiment:
    def __init__(self, a, b, identities):
        self.urls = {"A": a.rstrip("/"), "B": b.rstrip("/")}
        config = json.loads(pathlib.Path(identities).read_text(encoding="utf-8-sig"))
        self.tokens = {(p["tenant"], p["subject"]): p["token"] for p in config["principals"]}
        self.rows = []
        self.lock = threading.Lock()
        self.prefix = "demo." + uuid.uuid4().hex
        self.canary = "SYNTHETIC-DEMO-TEXT-" + uuid.uuid4().hex

    def call(self, phase, instance, path, payload=None, subject="alice", tenant="acme", expected=200):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(self.urls[instance] + path, data=data, headers={
            "Authorization": "Bearer " + self.tokens[tenant, subject], "Content-Type": "application/json"})
        started = time.perf_counter()
        try:
            response = urllib.request.urlopen(request, timeout=20)
        except urllib.error.HTTPError as failure:
            response = failure
        with response:
            status, headers = response.status, response.headers
            raw = response.read().decode("utf-8")
        elapsed = round((time.perf_counter() - started) * 1000, 2)
        result = json.loads(raw)
        code = result.get("code", result.get("outcome", "OK"))
        safe = status < 400 or (self.canary not in raw and not result.get("items"))
        passed = status == expected and safe and "no-store" in headers.get("Cache-Control", "")
        with self.lock:
            self.rows.append({"phase": phase, "instance": instance, "status": status, "code": code,
                              "elapsed_ms": elapsed, "expected_status": expected, "passed": passed})
        if not passed:
            raise RuntimeError("EXPERIMENT_ASSERTION_FAILED:" + phase)
        return result

    def event(self, source, sequence, readers, content=None, state="ACTIVE", fresh=None):
        return {"source_id": source, "sequence": sequence, "content": self.canary if content is None else content,
                "readers": readers, "state": state,
                "fresh_until": fresh or (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=240)).isoformat()}

    def run(self):
        for node in self.urls:
            self.call("ready", node, "/health/ready")
        source = self.prefix + ".policy"
        original = self.event(source, 1, ["alice", "bob"])
        self.call("seed", "B", "/v1/source-events", original, subject="writer")
        root = self.call("create-source", "A", "/v1/contexts/source", {"source_id": source}, expected=201)
        child = self.call("derive", "A", "/v1/contexts/derived",
                          {"content": self.canary + ":summary", "parent_ids": [root["id"]]}, expected=201)
        grand = self.call("derive-again", "B", "/v1/contexts/derived",
                          {"content": self.canary + ":summary-2", "parent_ids": [child["id"]]}, expected=201)
        ids = {"context_ids": [root["id"], child["id"], grand["id"]]}
        for node in self.urls:
            for _ in range(2):
                self.call("warm", node, "/v1/contexts/assemble", ids)
        self.call("revoke-confirmed", "B", "/v1/source-events", self.event(source, 2, ["bob"]), subject="writer")
        barrier = threading.Event()
        def denied_read(i):
            barrier.wait(timeout=10)
            return self.call("after-revoke", "A" if i % 2 == 0 else "B", "/v1/contexts/assemble", ids, expected=403)
        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
            futures = [pool.submit(denied_read, i) for i in range(50)]
            barrier.set()
            for future in futures:
                future.result(timeout=30)
        self.call("replay-old", "B", "/v1/source-events", original, subject="writer")
        denied = self.call("old-replay-still-denied", "A", "/v1/contexts/assemble", ids, expected=403)
        self.call("denial-receipt", "A", "/v1/receipts/" + denied["receipt"]["id"])
        self.call("sequence-conflict", "B", "/v1/source-events", self.event(source, 2, ["alice"]), subject="writer", expected=409)
        self.call("regrant", "B", "/v1/source-events", self.event(source, 3, ["alice", "bob"]), subject="writer")
        self.call("old-epoch", "A", "/v1/contexts/assemble", ids, expected=409)
        fresh = self.call("new-epoch", "A", "/v1/contexts/source", {"source_id": source}, expected=201)
        child2 = self.call("new-derived", "A", "/v1/contexts/derived",
                           {"content": self.canary + ":new-summary", "parent_ids": [fresh["id"]]}, expected=201)
        current = {"context_ids": [fresh["id"], child2["id"]]}
        self.call("new-context-readable", "A", "/v1/contexts/assemble", current)
        self.call("content-update", "B", "/v1/source-events", self.event(source, 4, ["alice", "bob"], self.canary + ":v2"), subject="writer")
        self.call("descendants-stale", "A", "/v1/contexts/assemble", current, expected=409)
        self.call("delete", "B", "/v1/source-events", self.event(source, 5, [], "", "DELETED"), subject="writer")
        self.call("deleted-context", "A", "/v1/contexts/assemble", ids, expected=410)
        self.call("cross-tenant", "B", "/v1/contexts/assemble", ids, tenant="beta", expected=404)
        self.call("forged-identity", "A", "/v1/contexts/source", {"source_id": source, "tenant": "beta"}, expected=400)
        source2 = self.prefix + ".freshness"
        self.call("fresh-seed", "B", "/v1/source-events", self.event(source2, 1, ["alice"]), subject="writer")
        item2 = self.call("fresh-context", "A", "/v1/contexts/source", {"source_id": source2}, expected=201)
        expired = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)).isoformat()
        self.call("freshness-expired", "B", "/v1/source-events", self.event(source2, 2, ["alice"], fresh=expired), subject="writer")
        self.call("unverified-blocked", "A", "/v1/contexts/assemble", {"context_ids": [item2["id"]]}, expected=503)
        self.call("renew", "B", "/v1/source-events", self.event(source2, 3, ["alice"]), subject="writer")
        self.call("renew-same-context", "A", "/v1/contexts/assemble", {"context_ids": [item2["id"]]})
        self.call("all-or-nothing", "A", "/v1/contexts/assemble", {"context_ids": [item2["id"], root["id"]]}, expected=410)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--a", default="http://127.0.0.1:58091")
    parser.add_argument("--b", default="http://127.0.0.1:58092")
    parser.add_argument("--identities", default=".local/identities.json")
    parser.add_argument("--output", default="artifacts/local/demo.json")
    args = parser.parse_args()
    try:
        experiment = Experiment(args.a, args.b, args.identities)
        experiment.run()
        rows = experiment.rows
        report = {"project": "ContextFence", "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                  "transport": "two independent HTTP endpoints", "samples": len(rows),
                  "post_revoke_samples": sum(r["phase"] == "after-revoke" for r in rows),
                  "all_passed": all(r["passed"] for r in rows),
                  "cache_setup": "Two consecutive successful reads on each instance; actual hit counters are checked by integration tests.",
                  "latency_scope": "Local experiment only; includes network and database queue time, not a production benchmark.",
                  "observations": rows}
        output = pathlib.Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("PASS: {} HTTP observations; 50/50 post-revocation requests rejected. Report: {}".format(len(rows), output))
        return 0
    except Exception as failure:
        # urllib errors/config exceptions may contain secrets or URLs. Never dump their text or response body.
        print("FAIL: {}. Check local services and identity configuration; no report marked successful.".format(type(failure).__name__), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
