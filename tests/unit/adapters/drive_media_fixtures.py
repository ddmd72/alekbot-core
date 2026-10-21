"""Fixtures for the media extractor tests: a throwaway CA and a local HTTPS server (with Range) built at test time
into tmp_path — never committed key material — and ffmpeg-made media. Synthetic data only."""
import datetime
import http.server
import ipaddress
import shutil
import ssl
import subprocess
import threading
from typing import Dict, Optional, Tuple

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _name(common: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])


def make_ca_and_server_cert(directory) -> Tuple[str, str, str]:
    """(ca_pem, server_cert_pem, server_key_pem) paths; the server cert is for localhost / 127.0.0.1."""
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_cert = (x509.CertificateBuilder().subject_name(_name("test-ca")).issuer_name(_name("test-ca"))
               .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=2))
               .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
               .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder().subject_name(_name("localhost")).issuer_name(ca_cert.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    paths = []
    for name, obj in (("ca.pem", ca_cert), ("server.pem", cert)):
        path = directory / name
        path.write_bytes(obj.public_bytes(serialization.Encoding.PEM))
        paths.append(str(path))
    key_path = directory / "server.key"
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return paths[0], paths[1], str(key_path)


class MediaServer:
    """HTTPS on localhost; `files` maps a path to bytes. Honours `Range: bytes=a-b` like the Graph download URL."""

    def __init__(self, directory, files: Optional[Dict[str, bytes]] = None) -> None:
        self.ca_pem, cert, key = make_ca_and_server_cert(directory)
        self.files: Dict[str, bytes] = dict(files or {})
        self.requests = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _serve(self, body_too: bool) -> None:
                data = owner.files.get(self.path.split("?")[0])
                owner.requests.append((self.command, self.path, self.headers.get("Range")))
                if data is None:
                    self.send_error(404)
                    return
                start, end, status = 0, len(data) - 1, 200
                rng = self.headers.get("Range")
                if rng and rng.startswith("bytes="):
                    first, _, last = rng[6:].partition("-")
                    start = int(first) if first else max(0, len(data) - int(last))
                    end = int(last) if first and last else len(data) - 1
                    end = min(end, len(data) - 1)
                    status = 206
                body = data[start:end + 1]
                self.send_response(status)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(len(body)))
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                self.end_headers()
                if body_too:
                    try:
                        self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError):
                        pass

            def do_GET(self):
                self._serve(True)

            def do_HEAD(self):
                self._serve(False)

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        self._httpd.socket = context.wrap_socket(self._httpd.socket, server_side=True)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> "MediaServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def url(self, path: str) -> str:
        return f"https://localhost:{self.port}{path}"


def _ffmpeg(*args: str, capture: bool = False) -> bytes:
    done = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
                          check=True, stdout=subprocess.PIPE if capture else subprocess.DEVNULL)
    return done.stdout


def make_audio_mp4(path, seconds: int) -> bytes:
    """A sine tone in AAC / mp4 (moov at the end, ordinary)."""
    _ffmpeg("-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "aac", str(path))
    return path.read_bytes()


def make_cueless_webm(seconds: int) -> bytes:
    """What a browser recorder produces: webm/Opus written to a pipe, so no Duration and no Cues."""
    return _ffmpeg("-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "libopus",
                   "-f", "webm", "pipe:1", capture=True)


def make_silent_video_mp4(path, seconds: int) -> bytes:
    """A 16x16 black video at 1 fps with silent mono audio: a large duration for few bytes."""
    _ffmpeg("-f", "lavfi", "-i", "color=c=black:s=16x16:r=1", "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono",
            "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-g", "600", "-c:a", "aac", "-b:a", "8k",
            "-movflags", "+faststart", str(path))
    return path.read_bytes()
