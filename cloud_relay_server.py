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
DANGER_PING_TTL_SEC = 45.0
MAX_DANGER_PINGS = 8

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
rooms = {}
lock = threading.RLock()


def now():
    return time.time()


def clean_text(value, max_len):
    return str(value or "").strip()[:max_len]


def clean_room_code(value):
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()[:12]


def clean_color(value, fallback="#00EBFF"):
    text = str(value or "").strip().upper()
    if re.fullmatch(r"#[0-9A-F]{6}", text):
        return text
    return str(fallback).upper()


def new_room_code():
    with lock:
        for _ in range(100):
            code = "".join(secrets.choice(ALPHABET) for _ in range(ROOM_CODE_LEN))
            if code not in rooms:
                return code
    raise RuntimeError("방 코드를 생성하지 못했습니다.")


def make_member(nickname, color="#00EBFF"):
    t = now()
    return {
        "member_id": uuid.uuid4().hex[:16],
        "token": secrets.token_urlsafe(24),
        "nickname": nickname,
        "color": clean_color(color),
        "lat": None,
        "long": None,
        "updated_at": None,
        "last_seen": t,
        "joined_at": t,
        "path": [],
    }


def public_member(m):
    return {
        "member_id": m["member_id"],
        "nickname": m["nickname"],
        "color": clean_color(m.get("color")),
        "lat": m["lat"],
        "long": m["long"],
        "updated_at": m["updated_at"],
        "path": m["path"],
    }


def _prune_danger_pings(room, t=None):
    t = now() if t is None else float(t)
    active = [p for p in room.get("danger_pings", []) if float(p.get("expires_at", 0) or 0) > t]
    if len(active) != len(room.get("danger_pings", [])):
        room["danger_pings"] = active
        room["danger_ping_revision"] = int(room.get("danger_ping_revision", 0)) + 1
    return active


def _ensure_owner(room):
    owner_id = str(room.get("owner_member_id") or "")
    if owner_id and owner_id in room.get("members", {}):
        return owner_id
    members = list(room.get("members", {}).values())
    if not members:
        room["owner_member_id"] = ""
        return ""
    members.sort(key=lambda m: float(m.get("joined_at", 0) or 0))
    room["owner_member_id"] = members[0]["member_id"]
    return room["owner_member_id"]


def cleanup():
    t = now()
    with lock:
        dead_rooms = []
        for code, room in rooms.items():
            _prune_danger_pings(room, t)
            dead_members = [
                mid for mid, m in room["members"].items()
                if t - m.get("last_seen", 0) > MEMBER_EXPIRE_SEC
            ]
            for mid in dead_members:
                room["members"].pop(mid, None)
            _ensure_owner(room)
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
    server_version = "TheIsleRelayCloud/1.4"

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
                "version": "1.4",
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
            elif path == "/api/color":
                self.update_color(payload)
            elif path == "/api/state":
                self.get_state(payload)
            elif path == "/api/clear_paths":
                self.clear_paths(payload)
            elif path == "/api/destination":
                self.update_destination(payload)
            elif path == "/api/destination_lock":
                self.update_destination_lock(payload)
            elif path == "/api/danger_ping":
                self.update_danger_ping(payload)
            elif path == "/api/leave":
                self.leave_room(payload)
            else:
                self._send(404, {"error": "Not found"})
        except Exception as e:
            print("server error:", repr(e))
            self._send(500, {"error": "서버 내부 오류가 발생했습니다."})

    def create_room(self, payload):
        nickname = clean_text(payload.get("nickname"), MAX_NICKNAME_LEN)
        color = clean_color(payload.get("color"))
        if not nickname:
            self._send(400, {"error": "닉네임을 입력해 주세요."})
            return

        code = new_room_code()
        member = make_member(nickname, color)
        with lock:
            rooms[code] = {
                "created_at": now(),
                "last_activity": now(),
                "members": {member["member_id"]: member},
                "owner_member_id": member["member_id"],
                # 방 전체 공유 목적지. None이면 목적지 없음.
                "destination": None,
                "destination_revision": 0,
                "destination_locked": False,
                "destination_lock_revision": 0,
                "path_revision": 0,
                "danger_pings": [],
                "danger_ping_revision": 0,
            }
        self._send(200, {
            "ok": True,
            "room_code": code,
            "member_id": member["member_id"],
            "token": member["token"],
            "owner_member_id": member["member_id"],
            "destination_locked": False,
        })

    def join_room(self, payload):
        nickname = clean_text(payload.get("nickname"), MAX_NICKNAME_LEN)
        color = clean_color(payload.get("color"))
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
            replaced_owner = str(room.get("owner_member_id") or "") in old_ids
            for mid in old_ids:
                room["members"].pop(mid, None)

            if len(room["members"]) >= MAX_ROOM_MEMBERS:
                self._send(409, {"error": f"방 인원은 최대 {MAX_ROOM_MEMBERS}명입니다."})
                return

            member = make_member(nickname, color)
            room["members"][member["member_id"]] = member
            if replaced_owner:
                room["owner_member_id"] = member["member_id"]
            owner_member_id = _ensure_owner(room)
            destination_locked = bool(room.get("destination_locked", False))
            room["last_activity"] = now()

        self._send(200, {
            "ok": True,
            "room_code": code,
            "member_id": member["member_id"],
            "token": member["token"],
            "owner_member_id": owner_member_id,
            "destination_locked": destination_locked,
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
            if payload.get("color") is not None:
                member["color"] = clean_color(payload.get("color"), member.get("color") or "#00EBFF")
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

    def update_color(self, payload):
        """사용자가 고른 파티 마커 색상을 저장하고 모든 멤버 state에 공유한다."""
        room, member, error = auth(payload)
        if error:
            self._send(401 if room else 404, {"error": error})
            return
        color = clean_color(payload.get("color"), member.get("color") or "#00EBFF")
        t = now()
        with lock:
            member["color"] = color
            member["last_seen"] = t
            room["last_activity"] = t
        self._send(200, {"ok": True, "color": color, "server_time": t})

    def get_state(self, payload):
        room, member, error = auth(payload)
        if error:
            self._send(401 if room else 404, {"error": error})
            return
        with lock:
            t = now()
            members = [public_member(m) for m in room["members"].values()]
            destination = room.get("destination")
            destination_revision = int(room.get("destination_revision", 0))
            path_revision = int(room.get("path_revision", 0))
            owner_member_id = _ensure_owner(room)
            destination_locked = bool(room.get("destination_locked", False))
            destination_lock_revision = int(room.get("destination_lock_revision", 0))
            danger_pings = list(_prune_danger_pings(room, t))
            danger_ping_revision = int(room.get("danger_ping_revision", 0))
        self._send(200, {
            "ok": True,
            "room_code": clean_room_code(payload.get("room_code")),
            "server_time": t,
            "members": members,
            "destination": destination,
            "destination_revision": destination_revision,
            "destination_locked": destination_locked,
            "destination_lock_revision": destination_lock_revision,
            "owner_member_id": owner_member_id,
            "path_revision": path_revision,
            "danger_pings": danger_pings,
            "danger_ping_revision": danger_ping_revision,
        })

    def clear_paths(self, payload):
        """모든 멤버의 이동 경로만 지운다. 현재 lat/long 좌표는 유지한다."""
        room, member, error = auth(payload)
        if error:
            self._send(401 if room else 404, {"error": error})
            return

        t = now()
        with lock:
            cleared = 0
            for m in room["members"].values():
                if m.get("lat") is not None and m.get("long") is not None:
                    m["path"] = [{
                        "lat": float(m["lat"]),
                        "long": float(m["long"]),
                        "t": float(m.get("updated_at") or t),
                    }]
                else:
                    m["path"] = []
                cleared += 1
            room["path_revision"] = int(room.get("path_revision", 0)) + 1
            path_revision = room["path_revision"]
            room["last_activity"] = t

        self._send(200, {
            "ok": True,
            "cleared_members": cleared,
            "path_revision": path_revision,
            "server_time": t,
        })

    def update_destination(self, payload):
        """방 전체 목적지를 설정/삭제한다. 마지막 요청이 방의 공유 목적지가 된다."""
        room, member, error = auth(payload)
        if error:
            self._send(401 if room else 404, {"error": error})
            return

        action = clean_text(payload.get("action"), 12).lower()
        t = now()

        with lock:
            owner_member_id = _ensure_owner(room)
            if bool(room.get("destination_locked", False)) and member["member_id"] != owner_member_id:
                self._send(403, {"error": "목적지가 잠겨 있습니다. 방장만 목적지를 변경할 수 있습니다."})
                return

        if action == "clear":
            with lock:
                room["destination"] = None
                room["destination_revision"] = int(room.get("destination_revision", 0)) + 1
                revision = room["destination_revision"]
                room["last_activity"] = t
            self._send(200, {
                "ok": True,
                "destination": None,
                "destination_revision": revision,
                "server_time": t,
            })
            return

        if action != "set":
            self._send(400, {"error": "action은 set 또는 clear가 필요합니다."})
            return

        try:
            lat = float(payload.get("lat"))
            long = float(payload.get("long"))
        except (TypeError, ValueError):
            self._send(400, {"error": "목적지 lat/long 값이 필요합니다."})
            return
        if not (-2000 <= lat <= 2000 and -2000 <= long <= 2000):
            self._send(400, {"error": "목적지 좌표 범위를 벗어났습니다."})
            return

        with lock:
            room["destination_revision"] = int(room.get("destination_revision", 0)) + 1
            revision = room["destination_revision"]
            room["destination"] = {
                "lat": lat,
                "long": long,
                "set_by_member_id": member["member_id"],
                "set_by_nickname": member["nickname"],
                "updated_at": t,
            }
            destination = room["destination"]
            room["last_activity"] = t

        self._send(200, {
            "ok": True,
            "destination": destination,
            "destination_revision": revision,
            "server_time": t,
        })

    def update_destination_lock(self, payload):
        """방장만 목적지 잠금을 켜거나 끌 수 있다."""
        room, member, error = auth(payload)
        if error:
            self._send(401 if room else 404, {"error": error})
            return

        locked = bool(payload.get("locked", False))
        t = now()
        with lock:
            owner_member_id = _ensure_owner(room)
            if member["member_id"] != owner_member_id:
                self._send(403, {"error": "방장만 목적지 잠금을 변경할 수 있습니다."})
                return
            room["destination_locked"] = locked
            room["destination_lock_revision"] = int(room.get("destination_lock_revision", 0)) + 1
            revision = room["destination_lock_revision"]
            room["last_activity"] = t

        self._send(200, {
            "ok": True,
            "destination_locked": locked,
            "destination_lock_revision": revision,
            "owner_member_id": owner_member_id,
            "server_time": t,
        })

    def update_danger_ping(self, payload):
        """공유 위험 핑 추가/전체 삭제. 핑은 서버 시간 기준 45초 뒤 자동 만료된다."""
        room, member, error = auth(payload)
        if error:
            self._send(401 if room else 404, {"error": error})
            return

        action = clean_text(payload.get("action"), 16).lower()
        t = now()
        with lock:
            _prune_danger_pings(room, t)

            if action == "clear_all":
                room["danger_pings"] = []
                room["danger_ping_revision"] = int(room.get("danger_ping_revision", 0)) + 1
            elif action == "add":
                try:
                    lat = float(payload.get("lat"))
                    long = float(payload.get("long"))
                except (TypeError, ValueError):
                    self._send(400, {"error": "위험 핑 lat/long 값이 필요합니다."})
                    return
                if not (-2000 <= lat <= 2000 and -2000 <= long <= 2000):
                    self._send(400, {"error": "위험 핑 좌표 범위를 벗어났습니다."})
                    return
                ping = {
                    "ping_id": uuid.uuid4().hex[:12],
                    "lat": lat,
                    "long": long,
                    "set_by_member_id": member["member_id"],
                    "set_by_nickname": member["nickname"],
                    "created_at": t,
                    "expires_at": t + DANGER_PING_TTL_SEC,
                }
                room["danger_pings"].append(ping)
                if len(room["danger_pings"]) > MAX_DANGER_PINGS:
                    room["danger_pings"] = room["danger_pings"][-MAX_DANGER_PINGS:]
                room["danger_ping_revision"] = int(room.get("danger_ping_revision", 0)) + 1
            else:
                self._send(400, {"error": "action은 add 또는 clear_all이 필요합니다."})
                return

            room["last_activity"] = t
            pings = list(room.get("danger_pings", []))
            revision = int(room.get("danger_ping_revision", 0))

        self._send(200, {
            "ok": True,
            "danger_pings": pings,
            "danger_ping_revision": revision,
            "server_time": t,
        })

    def leave_room(self, payload):
        room, member, error = auth(payload)
        if error:
            self._send(200, {"ok": True})
            return
        code = clean_room_code(payload.get("room_code"))
        with lock:
            room["members"].pop(member["member_id"], None)
            _ensure_owner(room)
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
