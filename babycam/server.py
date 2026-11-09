"""집 와이파이 전용 베이비캠 서버 (Windows용, 파이썬 표준 라이브러리만 사용).

- 웹캠 PC:  http://localhost:8000/cam  (브라우저로 열어 두기)
- 폰:       http://<PC의 IP>:8000      (같은 와이파이에서 접속)
영상/소리는 WebRTC로 PC → 폰에 직접 전송되고, 이 서버는 연결 정보만 중계합니다.
"""
import base64
import json
import os
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("BABYCAM_PORT", "8000"))
HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
PASSWORD_FILE = os.path.join(HERE, "password.txt")
POLL_TIMEOUT = 25
VIEWER_EXPIRE = 90


def load_password():
    if not os.path.exists(PASSWORD_FILE):
        with open(PASSWORD_FILE, "w", encoding="utf-8") as f:
            f.write(f"{secrets.randbelow(900000) + 100000}\n")
    with open(PASSWORD_FILE, encoding="utf-8") as f:
        return f.read().strip()


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # 실제로 전송하지는 않음
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def keep_pc_awake():
    """Windows가 절전 모드로 들어가지 않게 함 (서버가 켜져 있는 동안)."""
    if sys.platform == "win32":
        import ctypes

        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)


class Hub:
    """'cam' 또는 시청자 id별 메시지 큐 (long-polling)."""

    def __init__(self):
        self.cond = threading.Condition()
        self.queues = {}
        self.seen = {}
        self.gen = {}  # 같은 who의 새 요청이 오면 이전(끊긴 페이지의) 요청은 양보

    def _prune(self):
        now = time.time()
        for who, t in list(self.seen.items()):
            if who != "cam" and now - t > VIEWER_EXPIRE:
                self.seen.pop(who, None)
                self.queues.pop(who, None)
                self.gen.pop(who, None)

    def push(self, to, msg):
        with self.cond:
            self.queues.setdefault(to, []).append(msg)
            self.cond.notify_all()

    def pull(self, who):
        with self.cond:
            self._prune()
            self.seen[who] = time.time()
            my_gen = self.gen[who] = self.gen.get(who, 0) + 1
            self.cond.notify_all()
            end = time.time() + POLL_TIMEOUT
            while not self.queues.get(who):
                remaining = end - time.time()
                if remaining <= 0 or self.gen.get(who) != my_gen:
                    return []
                self.cond.wait(remaining)
            if self.gen.get(who) != my_gen:
                return []
            msgs = self.queues.get(who, [])
            self.queues[who] = []
            self.seen[who] = time.time()
            return msgs

    def requeue(self, who, msgs):
        with self.cond:
            self.queues[who] = msgs + self.queues.get(who, [])
            self.cond.notify_all()

    def cam_restart(self):
        with self.cond:
            self.queues["cam"] = []
            for who in self.seen:
                if who != "cam":
                    self.queues.setdefault(who, []).append({"type": "cam-restart"})
            self.cond.notify_all()

    def viewer_count(self):
        with self.cond:
            return sum(1 for w in self.seen if w != "cam")


HUB = Hub()
PASSWORD = ""


class Handler(BaseHTTPRequestHandler):
    server_version = "BabyCam"

    def log_message(self, *args):
        pass

    # ---- helpers ----
    def is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def authorized(self):
        if self.is_local():
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                _, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
                return secrets.compare_digest(pw, PASSWORD)
            except Exception:
                return False
        return False

    def send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj):
        self.send(200, json.dumps(obj).encode(), "application/json")

    def send_file(self, name):
        with open(os.path.join(STATIC, name), "rb") as f:
            self.send(200, f.read(), "text/html; charset=utf-8")

    def guard(self):
        if self.authorized():
            return True
        self.send(401, "비밀번호가 필요합니다".encode(), extra={
            "WWW-Authenticate": 'Basic realm="BabyCam", charset="UTF-8"'})
        return False

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 200_000:
            raise ValueError("too large")
        return json.loads(self.rfile.read(length) or b"{}")

    # ---- routes ----
    def do_GET(self):
        if not self.guard():
            return
        url = urlparse(self.path)
        if url.path in ("/", "/view"):
            return self.send_file("view.html")
        if url.path == "/cam":
            if not self.is_local():
                return self.send(403, "카메라 페이지는 웹캠 PC에서만 열 수 있습니다".encode())
            return self.send_file("cam.html")
        if url.path == "/api/poll":
            who = parse_qs(url.query).get("who", [""])[0][:64]
            if not who or (who == "cam" and not self.is_local()):
                return self.send(400)
            msgs = HUB.pull(who)
            try:
                return self.send_json(msgs)
            except OSError:  # 페이지가 닫힌 경우 메시지를 되돌려 놓음
                if msgs:
                    HUB.requeue(who, msgs)
                return
        if url.path == "/api/status":
            return self.send_json({"viewers": HUB.viewer_count()})
        self.send(404, b"not found")

    def do_POST(self):
        if not self.guard():
            return
        url = urlparse(self.path)
        try:
            if url.path == "/api/send":
                data = self.read_json()
                to = str(data.get("to", ""))[:64]
                # 시청자는 카메라에게만 보낼 수 있음
                if not to or (to != "cam" and not self.is_local()):
                    return self.send(400)
                HUB.push(to, data.get("msg"))
                return self.send_json({"ok": True})
            if url.path == "/api/cam-hello" and self.is_local():
                HUB.cam_restart()
                return self.send_json({"ok": True})
        except (ValueError, json.JSONDecodeError):
            return self.send(400)
        self.send(404, b"not found")


def main():
    global PASSWORD
    PASSWORD = load_password()
    keep_pc_awake()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    ip = lan_ip()
    print("=" * 52)
    print("  베이비캠 서버 실행 중  (끄려면 이 창을 닫으세요)")
    print("=" * 52)
    print(f"  1) 이 PC에서 열기 : http://localhost:{PORT}/cam")
    print(f"  2) 폰에서 열기    : http://{ip}:{PORT}")
    print(f"     사용자 이름: 아무거나 / 비밀번호: {PASSWORD}")
    print(f"     (비밀번호 변경: {PASSWORD_FILE})")
    print("=" * 52)
    if not os.environ.get("BABYCAM_NO_BROWSER"):
        import webbrowser
        threading.Timer(1.0, webbrowser.open, [f"http://localhost:{PORT}/cam"]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
