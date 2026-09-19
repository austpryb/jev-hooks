"""Write a 2000-prompt transcript for the triage bound test."""
import json, sys
with open(sys.argv[1], "w") as f:
    for i in range(2000):
        f.write(json.dumps({"type": "user", "message": {"role": "user", "content": f"decision number {i}: always use port {55000+i} for service {i} and never the other one"}}) + "\n")
