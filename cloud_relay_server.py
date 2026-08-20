import json
import os
import re
import secrets
import string
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8765"))

ROOM_CODE_LEN = 6
MAX_NICKNAME_LEN = 20
MAX_ROOM_MEMBERS = 16
MAX_PATH_POINTS = 120
PATH_MIN_DISTANCE = 0.10
MEMBER_EXPIRE_SEC = 60 * 60
ROOM_EXPIRE_SEC = 6 * 60 * 60

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
rooms = {}
lock = threading.RLock()


def now():
    return time.time()


def clean_text(value, max_len):
    return str(value or "").strip()[:max_len]


def clean_room_code(value):
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()[:12]


def new_room_code():
    with lock:
        for _ in range(100):
            code = "".join(secrets.choice(ALPHABET) for _ in range(ROOM_CODE_LEN))
            if code not in rooms:
                return code
    raise RuntimeError("방 코드를 생성하지 못했습니다.")


def make_member(nickname):
    t = now()
    return {
        "member_id": uuid.uuid4().hex[:16],
        "token": secrets.token_urlsafe(24),
        "nickname": nickname,
        "lat": None,
        "long": None,
        "updated_at": None,
        "last_seen": t,
        "path": [],
    }


def public_member(m):
    return {
        "member_id": m["member_id"],
        "nickname": m["nickname"],
        "lat": m["lat"],
        "long": m["long"],
        "updated_at": m["updated_at"],
        "path": m["path"],
    }


def cleanup():
    t = now()
    with lock:
        dead_rooms = []
        for code, room in rooms.items():
            dead_members = [
                mid for mid, m in room["members"].items()
                if t - m.get("last_seen", 0) > MEMBER_EXPIRE_SEC
            ]
            for mid in dead_members:
                room["members"].pop(mid, None)
            if not room["members"] and t - room.get("last_activity", t) > ROOM_EXPIRE_SEC:
                dead_rooms.append(code)
        for code in dead_rooms:
            rooms.pop(code, None)


def auth(payload):
    code = clean_room_code(payload.get("room_code"))
    member_id = clean_text(payload.get("member_id"), 64)
    token = clean_text(payload.get("token"), 128)
    with lock:
        room = rooms.get(code)
        if not room:
            return None, None, "방을 찾을 수 없습니다."
        member = room["members"].get(member_id)
        if not member or not secrets.compare_digest(member.get("token", ""), token):
            return None, None, "인증 정보가 올바르지 않습니다."
        member["last_seen"] = now()
        room["last_activity"] = now()
        return room, member, None


class Handler(BaseHTTPRequestHandler):
    server_version = "TheIsleRelayCloud/1.1"

    def log_message(self, fmt, *args):
        client_ip = self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {client_ip} - {fmt % args}", flush=True)

    def _send(self, status, body):
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(data)

    def _json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 64 * 1024:
                raise ValueError("요청이 너무 큽니다.")
            raw = self.rfile.read(length) if length else b"{}"
            obj = json.loads(raw.decode("utf-8"))
            if not isinstance(obj, dict):
                raise ValueError("JSON object가 필요합니다.")
            return obj
        except Exception as e:
            raise ValueError(f"잘못된 요청: {e}")

    def do_OPTIONS(self):
        self._send(200, {"ok": True})

    def do_GET(self):
        cleanup()
        if self.path.rstrip("/") in ("", "/health"):
            with lock:
                room_count = len(rooms)
                member_count = sum(len(r["members"]) for r in rooms.values())
            self._send(200, {
                "ok": True,
                "service": "The Isle MiniMap Relay",
                "rooms": room_count,
                "members": member_count,
                "time": now(),
            })
            return
        self._send(404, {"error": "Not found"})

    def do_POST(self):
        cleanup()
        try:
            payload = self._json()
        except ValueError as e:
            self._send(400, {"error": str(e)})
            return

        path = self.path.rstrip("/")
        try:
            if path == "/api/create":
                self.create_room(payload)
            elif path == "/api/join":
                self.join_room(payload)
            elif path == "/api/update":
                self.update_position(payload)
            elif path == "/api/state":
                self.get_state(payload)
            elif path == "/api/leave":
                self.leave_room(payload)
            else:
                self._send(404, {"error": "Not found"})
        except Exception as e:
            print("server error:", repr(e))
            self._send(500, {"error": "서버 내부 오류가 발생했습니다."})

    def create_room(self, payload):
        nickname = clean_text(payload.get("nickname"), MAX_NICKNAME_LEN)
        if not nickname:
            self._send(400, {"error": "닉네임을 입력해 주세요."})
            return

        code = new_room_code()
        member = make_member(nickname)
        with lock:
            rooms[code] = {
                "created_at": now(),
                "last_activity": now(),
                "members": {member["member_id"]: member},
            }
        self._send(200, {
            "ok": True,
            "room_code": code,
            "member_id": member["member_id"],
            "token": member["token"],
        })

    def join_room(self, payload):
        nickname = clean_text(payload.get("nickname"), MAX_NICKNAME_LEN)
        code = clean_room_code(payload.get("room_code"))
        if not nickname or not code:
            self._send(400, {"error": "닉네임과 방 코드가 필요합니다."})
            return

        with lock:
            room = rooms.get(code)
            if not room:
                self._send(404, {"error": "방을 찾을 수 없습니다."})
                return

            # 같은 닉네임으로 재접속하면 이전 세션을 교체한다.
            old_ids = [mid for mid, m in room["members"].items() if m["nickname"].casefold() == nickname.casefold()]
            for mid in old_ids:
                room["members"].pop(mid, None)

            if len(room["members"]) >= MAX_ROOM_MEMBERS:
                self._send(409, {"error": f"방 인원은 최대 {MAX_ROOM_MEMBERS}명입니다."})
                return

            member = make_member(nickname)
            room["members"][member["member_id"]] = member
            room["last_activity"] = now()

        self._send(200, {
            "ok": True,
            "room_code": code,
            "member_id": member["member_id"],
            "token": member["token"],
        })

    def update_position(self, payload):
        room, member, error = auth(payload)
        if error:
            self._send(401 if room else 404, {"error": error})
            return
        try:
            lat = float(payload.get("lat"))
            long = float(payload.get("long"))
        except (TypeError, ValueError):
            self._send(400, {"error": "lat/long 값이 필요합니다."})
            return
        if not (-2000 <= lat <= 2000 and -2000 <= long <= 2000):
            self._send(400, {"error": "좌표 범위를 벗어났습니다."})
            return

        t = now()
        with lock:
            member["lat"] = lat
            member["long"] = long
            member["updated_at"] = t
            member["last_seen"] = t

            path = member["path"]
            should_add = True
            if path:
                p = path[-1]
                d = ((lat - p["lat"]) ** 2 + (long - p["long"]) ** 2) ** 0.5
                should_add = d >= PATH_MIN_DISTANCE
            if should_add:
                path.append({"lat": lat, "long": long, "t": t})
                if len(path) > MAX_PATH_POINTS:
                    del path[:-MAX_PATH_POINTS]
            room["last_activity"] = t

        self._send(200, {"ok": True, "updated_at": t})

    def get_state(self, payload):
        room, member, error = auth(payload)
        if error:
            self._send(401 if room else 404, {"error": error})
            return
        with lock:
            members = [public_member(m) for m in room["members"].values()]
        self._send(200, {
            "ok": True,
            "room_code": clean_room_code(payload.get("room_code")),
            "server_time": now(),
            "members": members,
        })

    def leave_room(self, payload):
        room, member, error = auth(payload)
        if error:
            self._send(200, {"ok": True})
            return
        code = clean_room_code(payload.get("room_code"))
        with lock:
            room["members"].pop(member["member_id"], None)
            room["last_activity"] = now()
            if not room["members"]:
                rooms.pop(code, None)
        self._send(200, {"ok": True})


def main():
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("=" * 58, flush=True)
    print("The Isle MiniMap Cloud Relay Server", flush=True)
    print(f"Listening: http://{HOST}:{PORT}", flush=True)
    print("Health:    /health", flush=True)
    print("Cloud ready: Render PORT environment supported", flush=True)
    print("=" * 58, flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
