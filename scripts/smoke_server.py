"""End-to-end check against a running tomd server."""

import json
import re
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8791"

with urllib.request.urlopen(BASE + "/", timeout=10) as response:
    body = response.read().decode("utf-8")
token = re.search(r'const TOKEN = "([^"]+)"', body).group(1)
print("token acquired:", bool(token))

payload = Path("tests/fixtures/report.pdf").read_bytes()
boundary = "----tomdcheck"
parts = [
    f"--{boundary}\r\n".encode(),
    b'Content-Disposition: form-data; name="file"; filename="report.pdf"\r\n',
    b"Content-Type: application/pdf\r\n\r\n",
    payload,
    b"\r\n",
    f"--{boundary}\r\n".encode(),
    b'Content-Disposition: form-data; name="images"\r\n\r\n',
    b"1\r\n",
    f"--{boundary}--\r\n".encode(),
]
request = urllib.request.Request(
    BASE + "/api/convert",
    data=b"".join(parts),
    method="POST",
    headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "X-Tomd-Token": token,
    },
)
with urllib.request.urlopen(request, timeout=60) as response:
    result = json.loads(response.read())

print("format      :", result["format"], "via", result["converter"])
print("status      :", result["status"])
print("elapsed_ms  :", result["elapsed_ms"])
print("markdown    :", result["bytes"], "bytes")
print("warnings    :", [w["code"] for w in result["warnings"]])
print("has table   :", "| Metric | Q1 | Q2 |" in result["markdown"])
print("has heading :", "## Summary" in result["markdown"])
print()
print(result["markdown"][:400])

# scanned PDF: must be reported, not faked
payload = Path("tests/fixtures/scanned.pdf").read_bytes()
parts[3] = payload
request = urllib.request.Request(
    BASE + "/api/convert",
    data=b"".join(parts),
    method="POST",
    headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "X-Tomd-Token": token},
)
with urllib.request.urlopen(request, timeout=60) as response:
    scanned = json.loads(response.read())
print("\nscanned.pdf -> status:", scanned["status"])
print("scanned.pdf -> warnings:", [w["code"] for w in scanned["warnings"]])
print("scanned.pdf -> body empty:", scanned["markdown"].strip() == "")
