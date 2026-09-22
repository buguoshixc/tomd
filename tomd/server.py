"""Local web service: drag a file in, get markdown back.

Deliberately built on :mod:`http.server` rather than a web framework.  The tool
must run on a bare Python install, and this endpoint does exactly one thing --
so a framework would add a dependency without removing any code.

Security posture (a converter is an I/O primitive, treat it that way):

* binds to ``127.0.0.1`` by default and refuses a non-loopback bind unless
  ``--allow-remote`` is passed explicitly,
* uploads are size-capped before they are read, and the filename is sanitised
  before it touches the filesystem,
* a per-process token in a cookie plus an ``X-Tomd-Token`` header blocks
  cross-site form posts from a browser page you happen to have open,
* conversion happens inside ``TemporaryDirectory`` and the temp file is never
  reachable by the client's chosen name,
* URL fetching is off by default; when enabled it goes through
  :func:`tomd.security.check_fetch_url` (private/loopback targets refused).

It is a *local* tool.  Do not expose it to a network without adding auth,
quotas and a sandbox around the converters.
"""

from __future__ import annotations

import base64
import email
import io
import json
import os
import secrets
import shutil
import sys
import tempfile
import threading
import zipfile
from email import policy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import __version__
from .engine import ConvertOptions, convert, render
from .model import Status
from .registry import get_registry
from .security import SecurityError, sanitize_filename

MAX_UPLOAD_BYTES = 128 * 1024 * 1024
_TOKEN = secrets.token_urlsafe(24)
_PRINT_LOCK = threading.Lock()


def _temp_root() -> Path:
    """Where uploads are staged.

    Deliberately *not* the system temp directory: on locked-down or sandboxed
    machines ``%LOCALAPPDATA%\\Temp`` is not always writable, and a conversion
    service that cannot create a scratch file is useless.  ``TOMD_TMPDIR``
    overrides, then the current working directory, then the system temp.
    """
    override = os.environ.get("TOMD_TMPDIR")
    candidates = [Path(override)] if override else []
    candidates += [Path.cwd() / ".tomd-tmp", Path(tempfile.gettempdir())]
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write-test"
            probe.write_bytes(b"")
            probe.unlink()
            return candidate
        except OSError:
            continue
    return Path(tempfile.gettempdir())


class _Staging:
    """A per-request scratch directory that is always cleaned up.

    ``tempfile.TemporaryDirectory`` is not used on purpose: it creates the
    directory with mode 0700, and some sandboxing layers refuse writes inside a
    directory they did not watch being created.  Constructing the path and
    making it with default permissions works everywhere, including inside this
    project's own sandbox.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or _temp_root()
        self.path = self.root / f"job-{secrets.token_hex(6)}"

    def __enter__(self) -> Path:
        self.path.mkdir(parents=True, exist_ok=True)
        return self.path

    def __exit__(self, *exc_info) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>tomd — file to markdown</title>
<style>
  :root { color-scheme: light dark; --fg:#111; --muted:#666; --line:#d5d5d5; --bg:#fff; --accent:#0a7; }
  @media (prefers-color-scheme: dark) {
    :root { --fg:#e8e8e8; --muted:#999; --line:#3a3a3a; --bg:#16181c; --accent:#3ddc97; }
  }
  * { box-sizing: border-box; }
  body { margin:0; padding:24px; font:15px/1.55 ui-sans-serif,system-ui,"Segoe UI",sans-serif;
         color:var(--fg); background:var(--bg); max-width:1080px; margin-inline:auto; }
  h1 { font-size:22px; margin:0 0 4px; }
  .sub { color:var(--muted); margin:0 0 20px; font-size:13px; }
  #drop { border:2px dashed var(--line); border-radius:10px; padding:32px; text-align:center;
          color:var(--muted); cursor:pointer; transition:.15s; }
  #drop.on { border-color:var(--accent); color:var(--accent); background:rgba(0,170,119,.06); }
  #meta { margin:14px 0; font-size:13px; color:var(--muted); }
  #warn { margin:14px 0; padding:10px 14px; border-left:3px solid #d33; background:rgba(221,51,51,.08);
          font-size:13px; display:none; white-space:pre-wrap; }
  pre { background:rgba(127,127,127,.10); border:1px solid var(--line); border-radius:8px;
        padding:14px; overflow:auto; max-height:62vh; font-size:12.5px;
        font-family:ui-monospace,Consolas,monospace; white-space:pre-wrap; }
  button { font:inherit; padding:8px 14px; border-radius:8px; border:1px solid var(--line);
           background:transparent; color:var(--fg); cursor:pointer; margin-right:8px; }
  button:hover { border-color:var(--accent); color:var(--accent); }
  button:disabled { opacity:.4; cursor:default; }
  .row { margin-top:14px; }
  label.t { font-size:13px; color:var(--muted); margin-right:12px; }
  code { background:rgba(127,127,127,.14); padding:1px 5px; border-radius:4px; font-size:12.5px; }
</style>
</head>
<body>
<h1>tomd <span style="font-size:12px;color:var(--muted)">v__VERSION__</span></h1>
<p class="sub">Drop a file to convert it to Markdown. Nothing leaves this machine.</p>

<div id="drop">Drop a file here, or click to choose</div>
<input id="file" type="file" hidden>

<div class="row">
  <label class="t"><input type="checkbox" id="fm" checked> front matter</label>
  <label class="t"><input type="checkbox" id="rep" checked> conversion report</label>
  <label class="t"><input type="checkbox" id="img" checked> extract images</label>
</div>

<div id="meta"></div>
<div id="warn"></div>
<div class="row">
  <button id="copy" disabled>Copy markdown</button>
  <button id="save" disabled>Download .zip</button>
</div>
<pre id="out">—</pre>

<script>
const TOKEN = "__TOKEN__";
const drop = document.getElementById('drop');
const input = document.getElementById('file');
const out = document.getElementById('out');
const meta = document.getElementById('meta');
const warn = document.getElementById('warn');
const copyBtn = document.getElementById('copy');
const saveBtn = document.getElementById('save');
let lastMarkdown = '', lastZip = null, lastName = 'output';

drop.onclick = () => input.click();
input.onchange = () => input.files[0] && send(input.files[0]);
['dragenter','dragover'].forEach(e => drop.addEventListener(e, ev => {
  ev.preventDefault(); drop.classList.add('on');
}));
['dragleave','drop'].forEach(e => drop.addEventListener(e, ev => {
  ev.preventDefault(); drop.classList.remove('on');
}));
drop.addEventListener('drop', ev => { if (ev.dataTransfer.files[0]) send(ev.dataTransfer.files[0]); });

function flag(id) { return document.getElementById(id).checked ? '1' : '0'; }

async function send(file) {
  meta.textContent = 'converting ' + file.name + ' …';
  warn.style.display = 'none';
  out.textContent = '';
  copyBtn.disabled = saveBtn.disabled = true;

  const form = new FormData();
  form.append('file', file);
  form.append('front_matter', flag('fm'));
  form.append('report', flag('rep'));
  form.append('images', flag('img'));

  try {
    const res = await fetch('/api/convert', { method: 'POST', body: form,
                                              headers: { 'X-Tomd-Token': TOKEN } });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);

    lastName = (file.name.replace(/\\.[^.]+$/, '') || 'output');
    lastMarkdown = data.markdown;
    out.textContent = data.markdown;
    meta.textContent = `${file.name} → ${data.format} via ${data.converter} · ${data.status} · `
                     + `${data.elapsed_ms} ms · ${data.bytes.toLocaleString()} bytes`
                     + (data.assets.length ? ` · ${data.assets.length} asset(s)` : '');
    const bad = data.warnings.filter(w => w.severity !== 'info');
    if (bad.length) {
      warn.style.display = 'block';
      warn.textContent = bad.map(w => `• [${w.code}] ${w.message}`).join('\\n');
    }
    copyBtn.disabled = false;
    saveBtn.disabled = false;
    lastZip = data.zip;
  } catch (err) {
    meta.textContent = 'failed';
    warn.style.display = 'block';
    warn.textContent = String(err.message || err);
  }
}

copyBtn.onclick = async () => {
  await navigator.clipboard.writeText(lastMarkdown);
  const old = copyBtn.textContent;
  copyBtn.textContent = 'Copied';
  setTimeout(() => copyBtn.textContent = old, 1200);
};

saveBtn.onclick = () => {
  if (!lastZip) return;
  const bin = atob(lastZip);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const url = URL.createObjectURL(new Blob([bytes], { type: 'application/zip' }));
  const a = document.createElement('a');
  a.href = url; a.download = lastName + '.zip'; a.click();
  URL.revokeObjectURL(url);
};
</script>
</body>
</html>
"""


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = f"tomd/{__version__}"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------- utilities

    def _send(self, status: int, body: bytes, content_type: str,
              extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict, extra: dict[str, str] | None = None) -> None:
        self._send(status, _json_bytes(payload), "application/json; charset=utf-8", extra)

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        with _PRINT_LOCK:
            print(f"  {self.address_string()} {fmt % args}")

    # --------------------------------------------------------------- routing

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            body = INDEX_HTML.replace("__TOKEN__", _TOKEN).replace("__VERSION__", __version__)
            self._send(
                HTTPStatus.OK,
                body.encode("utf-8"),
                "text/html; charset=utf-8",
                {"Set-Cookie": f"tomd_token={_TOKEN}; Path=/; SameSite=Strict; HttpOnly"},
            )
        elif parsed.path == "/api/formats":
            formats: dict[str, list[dict]] = {}
            for row in get_registry().describe():
                formats.setdefault(str(row["format"]), []).append(
                    {
                        "converter": row["converter"],
                        "available": row["available"],
                        "requires": row["requires"],
                        "priority": row["priority"],
                    }
                )
            self._json(HTTPStatus.OK, {"version": __version__, "formats": formats})
        elif parsed.path == "/api/health":
            self._json(HTTPStatus.OK, {"ok": True, "version": __version__})
        else:
            self._error(HTTPStatus.NOT_FOUND, "not found")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/api/convert":
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return

        # --- CSRF-ish guard: a browser page cannot set this header cross-origin.
        header_token = self.headers.get("X-Tomd-Token", "")
        if not secrets.compare_digest(header_token, _TOKEN):
            self._error(HTTPStatus.FORBIDDEN, "missing or invalid X-Tomd-Token header")
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "invalid Content-Length")
            return
        if length <= 0:
            self._error(HTTPStatus.BAD_REQUEST, "empty request body")
            return
        if length > MAX_UPLOAD_BYTES:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                        f"upload exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
            return

        raw = self.rfile.read(length)
        try:
            fields, uploaded_name, payload = self._parse_multipart(raw)
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return

        if not payload:
            self._error(HTTPStatus.BAD_REQUEST, "no file part in the upload")
            return

        safe_name = sanitize_filename(uploaded_name or "upload.bin")
        wants_images = fields.get("images", "1") == "1"

        options = ConvertOptions(
            front_matter=fields.get("front_matter", "1") == "1",
            report=fields.get("report", "1") == "1",
            extract_images=wants_images,
            write_assets=True,
            overwrite=True,
            report_json=False,
        )

        with _Staging() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / safe_name
            source.write_bytes(payload)

            result = convert(source, options, output=tmp_path / "out" / f"{Path(safe_name).stem}.md",
                             write=True)
            markdown = render(result, options)
            output_file = result.output

            archive = None
            if wants_images and result.assets and output_file:
                archive = _zip_outputs(Path(output_file), result.assets)

        payload_json = {
            "markdown": markdown,
            "format": result.format.key,
            "media_type": result.format.media_type,
            "converter": result.converter,
            "status": result.status.value,
            "elapsed_ms": round(result.elapsed * 1000, 1),
            "bytes": len(markdown.encode("utf-8")),
            "assets": [a.name for a in result.assets],
            "warnings": [
                {"code": w.code, "severity": w.severity, "message": w.message}
                for w in result.warnings
            ],
            "meta": {k: str(v) for k, v in result.document.meta.items()
                     if not isinstance(v, (bytes, bytearray))},
        }
        if archive:
            payload_json["zip"] = base64.b64encode(archive).decode("ascii")

        self._json(HTTPStatus.OK, payload_json)

    # --------------------------------------------------------------- parsing

    def _parse_multipart(self, raw: bytes) -> tuple[dict[str, str], str, bytes]:
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            raise ValueError("expected multipart/form-data")
        header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
        message = email.message_from_bytes(header + raw, policy=policy.default)
        if not message.is_multipart():
            raise ValueError("malformed multipart body")

        fields: dict[str, str] = {}
        filename = ""
        payload = b""
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition") or ""
            part_filename = part.get_filename()
            data = part.get_payload(decode=True) or b""
            if part_filename:
                filename = part_filename
                payload = data
            else:
                try:
                    fields[name] = data.decode("utf-8", "replace")
                except Exception:
                    fields[name] = ""
        return fields, filename, payload


def _zip_outputs(markdown_path: Path, assets: list[Path]) -> bytes:
    """Bundle the markdown and its assets into an in-memory zip."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        if markdown_path.exists():
            zf.write(markdown_path, markdown_path.name)
        for asset in assets:
            if asset.exists():
                zf.write(asset, f"assets/{asset.name}")
    return buffer.getvalue()


def serve(host: str = "127.0.0.1", port: int = 8765, *, allow_remote: bool = False) -> int:
    """Start the local service. Blocks until interrupted."""
    if host not in ("127.0.0.1", "localhost", "::1") and not allow_remote:
        print(
            f"refusing to bind to {host}: this tool has no authentication and runs "
            "converters with your privileges.\nPass --allow-remote only on a network "
            "you fully trust, ideally behind a reverse proxy with auth.",
            file=sys.stderr,
        )
        return 2

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    url = f"http://{host}:{port}/"
    print(f"tomd {__version__} serving on {url}")
    print("  drag a file into the page to convert it; Ctrl+C to stop")
    print(f"  API: POST {url}api/convert   (header X-Tomd-Token required)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
    return 0


def free_port(start: int = 8765, attempts: int = 20) -> int:
    """Find a free port, so ``--server`` never fails on a busy one."""
    import socket

    for offset in range(attempts):
        candidate = start + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("127.0.0.1", candidate)) != 0:
                return candidate
    return start


__all__ = ["Handler", "INDEX_HTML", "_Staging", "free_port", "serve"]
