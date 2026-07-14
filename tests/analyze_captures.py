#!/usr/bin/env python3
"""Analyze captured request pairs — only OpenAI-format (test requests)."""
import hashlib, json, difflib, os
from pathlib import Path

CAP_DIR = os.environ.get("CAP_DIR", "./captures")

def hash_prefix(body, chars=None):
    raw = json.dumps(body, sort_keys=False)
    if chars: raw = raw[:chars]
    return hashlib.sha256(raw.encode()).hexdigest()

def split_hot_live(body):
    msgs = body.get("messages", [])
    hot = {"system": body.get("system"), "tools": body.get("tools"),
           "messages": msgs[:-1] if len(msgs) > 1 else msgs}
    live = {"messages": msgs[-1:] if msgs else []}
    return hot, live

def diff_report(name, a, b):
    ha, la = split_hot_live(a.get("body", a))
    hb, lb = split_hot_live(b.get("body", b))
    ha_s = json.dumps(ha, sort_keys=False, indent=2)
    hb_s = json.dumps(hb, sort_keys=False, indent=2)
    ident = ha_s == hb_s
    print(f"[{name}] hot zone identical: {ident}")
    print(f"[{name}] hot hash A: {hash_prefix(ha)}")
    print(f"[{name}] hot hash B: {hash_prefix(hb)}")
    print(f"[{name}] hot first500 A: {hash_prefix(ha, 500)}")
    print(f"[{name}] hot first500 B: {hash_prefix(hb, 500)}")
    if not ident:
        # Show first diff line
        diff = list(difflib.unified_diff(ha_s.splitlines(), hb_s.splitlines(), lineterm=""))
        print(f"[{name}] DIFF ({len(diff)} lines), first: {diff[0] if diff else '?'}")
    ld = json.dumps(la, sort_keys=False) != json.dumps(lb, sort_keys=False)
    print(f"[{name}] live differs: {ld}")
    print()

caps = []
for f in sorted(Path(CAP_DIR).glob("capture_*.json")):
    caps.append(json.loads(f.read_text()))
caps.sort(key=lambda c: c["capture_id"])

# Filter to OpenAI-format only (our test requests)
test_caps = [c for c in caps if "chat/completions" in c.get("path", "") and c.get("body_bytes", 0) > 0]
print(f"Total: {len(caps)}, OpenAI-format (test): {len(test_caps)}\n")

# Reduce to first occurrence of each unique body_hash (skip duplicates from retries)
seen_hashes = set()
unique = []
for c in test_caps:
    h = c.get("body_hash", "")
    if h and h not in seen_hashes:
        seen_hashes.add(h)
        unique.append(c)

print(f"Unique test requests: {len(unique)}")

# Sequential pairwise: adjacent unique captures are test pairs (A/B, F/G, etc.)
for i in range(0, len(unique)-1, 2):
    a, b = unique[i], unique[i+1]
    # Only diff if models match (same-model pairs)
    if a.get("model") == b.get("model"):
        name = f"Pair-c{a['capture_id']}-c{b['capture_id']}"
        print(f"--- {name}: {a['body_bytes']}B vs {b['body_bytes']}B {'✓' if a.get('body_hash') == b.get('body_hash') else '✗ payload-diff'} ---")
        diff_report(name, a, b)
    else:
        name = f"DiffModel-c{a['capture_id']}({a.get('model','?')})-c{b['capture_id']}({b.get('model','?')})"
        print(f"--- {name}: different models, expected cache miss ---")
        diff_report(name, a, b)

print("Done.")
