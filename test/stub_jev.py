"""A stand-in for api.typesafe.ai so hooks can be exercised without a key.
A test steers answers with markers anywhere in the request (usually inside the
state): [qid=yes] / [qid=no] for a noul (else 0.5), [qid=pick:<option>] for a
choice (else the first option), [qid=level:<n>] for a score (else the middle);
[stub=429once:<token>] makes the first request carrying that token a 429 with Retry-After: 0.
JEV_STUB_RECORD=<path> appends every raw request body there, so a test can assert
what a hook does not send.
Run: python3 test/stub_jev.py <port>
"""
import json, os, re, sys
from http.server import BaseHTTPRequestHandler, HTTPServer


def answer(qid, q, body_text):
    t = q["type"]
    m = re.search(r"\[" + re.escape(qid) + r"=([^\]]+)\]", body_text)
    steer = m.group(1) if m else ""
    if t == "noul":
        v = 1.0 if steer == "yes" else 0.0 if steer == "no" else 0.5
        return {"type": "noul", "noul": v}
    if t == "choice":
        opts = list(q["criteria"])
        pick = steer[5:] if steer.startswith("pick:") and steer[5:] in opts else opts[0]
        probs = {o: (0.9 if o == pick else 0.1 / max(1, len(opts) - 1)) for o in opts}
        return {"type": "choice", "choice": pick, "probabilities": probs, "confidence": 0.85}
    levels = q["criteria"]
    idx = int(steer[6:]) if steer.startswith("level:") else len(levels) // 2
    probs = {str(i): (0.9 if i == idx else 0.1 / max(1, len(levels) - 1)) for i in range(len(levels))}
    return {"type": "score", "score": float(idx), "legend": {str(i): l for i, l in enumerate(levels)},
            "probabilities": probs, "confidence": 0.85}


SEEN_429 = set()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        if self.headers.get("Authorization", "") != "Bearer stub-key":
            self.send_response(401); self.end_headers(); return
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        body = json.loads(raw); body_text = raw.decode()
        rec = os.environ.get("JEV_STUB_RECORD")   # a test asserting what is NOT sent needs the raw request
        if rec:
            open(rec, "a").write(body_text + "\n")
        m = re.search(r"\[stub=429once:([A-Za-z0-9_-]+)\]", body_text)
        if m and m.group(1) not in SEEN_429:
            SEEN_429.add(m.group(1))
            self.send_response(429); self.send_header("Retry-After", "0"); self.end_headers(); return
        out = {"model": "jev-stub", "answers": {k: answer(k, q, body_text) for k, q in body["questions"].items()},
               "usage": {"input_tokens": len(json.dumps(body)) // 4, "output_tokens": 0}}
        data = json.dumps(out).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
