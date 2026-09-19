"""Exit 0 when the loop state file holds a well-formed 6-entry window of distinct tools."""
import json, sys
d = json.load(open(sys.argv[1])); p = d["pairs"]
ok = len(p) == 6 and len({x["tool"] for x in p}) == 6 and all(x["tool"].startswith("T") for x in p)
sys.exit(0 if ok else 1)
