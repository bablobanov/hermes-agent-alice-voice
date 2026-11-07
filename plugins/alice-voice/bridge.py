"""Private Alice skill to an existing Hermes Telegram session."""

import hmac
import json
import logging
import os
import re
import sys
import types
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Lock, RLock, Thread
from typing import cast

logger = logging.getLogger("alice-voice")

ACK = "Запрос принят для обработки. Ответ будет в Telegram"
DENIED = "Запрос отклонён"
NOT_SENT = "Запрос не отправлен. Попробуйте позже"


def reply(text, end_session=False):
    return {"version": "1.0", "response": {"text": text, "end_session": end_session}}


def identifier(value):
    return isinstance(value, str) and 0 < len(value) <= 256


def is_paused():
    from agent.estop import is_engaged
    return is_engaged()


# A forced plugin re-discovery in a running gateway (reload-plugins from `hermes plugins enable/disable`,
# the Desktop switch, a tool re-scanning for a missing provider) unloads this plugin, which closes the
# listener, and registers it again. The engine does not call the new connect factory on the same
# Telegram client: it keys factories by plugin and __qualname__ and skips a known key. So the right to
# listen, which only the gateway's Telegram connect grants, is handed across the reload: an unload that
# closed a serving listener leaves it pending, and so does a connect that reaches an unloaded bridge
# (a factory list taken before the unload) while no successor is registered yet; with a successor, that
# connect starts the successor. The registration takes a pending right once. The state lives in
# sys.modules because the engine imports the plugin package again on a re-discovery, which would reset
# a module global. One owner per process: the plugin runs only in the default profile.
_HANDOVER = "_alice_voice_listener_handover"


def _handover():
    module = sys.modules.get(_HANDOVER)
    if module is None:
        fresh = types.ModuleType(_HANDOVER, "alice-voice: the right to listen across a plugin reload")
        fresh.lock, fresh.pending, fresh.current = Lock(), False, None
        module = sys.modules.setdefault(_HANDOVER, fresh)
    return module


def arm_handover():
    state = _handover()
    with state.lock:
        state.pending = True


class AliceBridge:
    def __init__(self, ctx, paused=None):
        self.ctx = ctx
        self.cache = OrderedDict()
        self.lock = RLock()
        self.server = self.thread = None
        self.unloaded = False  # set by close(), the engine's unload; unlike a disabled configuration
        self.paused = paused or is_paused
        self.session_key = ctx.get_config("session_key", "")
        self.skill_id = ctx.get_config("skill_id", "")
        self.owner_user_id = ctx.get_config("owner_user_id", "")
        self.port = ctx.get_config("port", 8788)
        secret = os.environ.get("ALICE_WEBHOOK_SECRET", "")
        self.secret_path = ("/alice/" + secret).encode()
        self.enabled = bool(ctx.get_config("enabled", False) is True and ctx.profile_name == "default"
                            and identifier(self.skill_id) and not self.skill_id.startswith("<")
                            and isinstance(self.session_key, str)
                            and re.fullmatch(r"agent:main:telegram:dm:[1-9][0-9]*", self.session_key)
                            and isinstance(self.owner_user_id, str)
                            and type(self.port) is int and 0 <= self.port <= 65535
                            and re.fullmatch(r"[0-9a-fA-F]{64}", secret) and len(set(secret)) >= 4)

    def handle(self, path, data):
        with self.lock:
            return self._handle(path, data)

    def _handle(self, path, data):
        if not self.enabled:
            return 503, reply("Запрос не отправлен. Мост выключен")
        if not hmac.compare_digest(path.encode(), self.secret_path):
            return 403, reply(DENIED)
        if not isinstance(data, dict) or data.get("version") != "1.0":
            return 400, reply(DENIED)
        session, request = data.get("session"), data.get("request")
        if not isinstance(session, dict) or not isinstance(request, dict):
            return 400, reply(DENIED)
        user = session.get("user")
        account = user.get("user_id") if isinstance(user, dict) else None
        if (session.get("skill_id") != self.skill_id or not identifier(account)
                or (self.owner_user_id and account != self.owner_user_id)):
            return 403, reply(DENIED)
        if (not identifier(session.get("session_id"))
                or type(session.get("message_id")) is not int or session["message_id"] < 0
                or request.get("type") != "SimpleUtterance"):
            return 400, reply(DENIED)
        command, original = request.get("command"), request.get("original_utterance")
        if not isinstance(command, str) or not isinstance(original, str):
            return 400, reply(DENIED)
        for value in (command, original):
            if len(value) > 4096:
                return 400, reply(DENIED)
            if value.lstrip().startswith("/") or any(ord(c) < 32 or ord(c) == 127 for c in value):
                return 403, reply(DENIED)
        text = original if original.strip() else command
        if (session.get("new") is True and not command.strip()) or not text.strip():
            return 200, reply("Назовите задачу для Hermes. Ответ будет в Telegram")
        if command.strip().casefold() in {"помощь", "что ты умеешь", "help"}:
            return 200, reply("Назовите задачу. Управление и подтверждения доступны только в Telegram")
        if command.strip().casefold() in {"выход", "хватит", "стоп", "exit"}:
            return 200, reply("До встречи", end_session=True)
        control = command.strip().casefold().rstrip("?.!")
        if control in {"ответ готов", "дальше"}:
            return 200, reply("Ответ будет в Telegram")
        if self.paused():
            return 503, reply("Запрос не отправлен. Агент на паузе")
        key = (self.skill_id, account, session["session_id"], session["message_id"])
        if key in self.cache:
            return self.cache[key]
        try:
            # Stock SDK: True acknowledges async scheduling, never delivery or completion.
            accepted = self.ctx.inject_message(text, role="user", session_key=self.session_key)
        except Exception:
            accepted = False
        if not accepted:
            return 503, reply(NOT_SENT)
        result = (200, reply(ACK))
        self.cache[key] = result
        if len(self.cache) > 32:
            self.cache.popitem(last=False)
        return result

    def on_telegram_connect(self, _native, _adapter):
        with self.lock:  # checked and started under one lock: close() cannot slip in between
            if not self.unloaded:
                self.start()
                return
        state = _handover()
        with state.lock:
            successor = state.current
            if successor is None or successor is self:
                state.pending = True  # the next registration takes it
                return
        successor.start_or_keep_pending()

    def start(self):
        with self.lock:
            if not self.enabled or self.server is not None:
                return
            self.server = AliceServer(("127.0.0.1", self.port), AliceHandler)
            self.server.bridge = self
            self.thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1},
                                 name="alice-voice", daemon=True)
            self.thread.start()

    def start_or_keep_pending(self):
        try:
            self.start()
        except OSError as exc:  # the port is taken: the next reload tries again, so does a new Telegram client
            arm_handover()
            logger.warning("alice-voice: listener not restored after the plugin reload (%s)", type(exc).__name__)

    def adopt(self):
        """In register(), once the engine accepted the unload callback: become this process's bridge and
        take a pending right to listen."""
        if not self.enabled:
            return
        state = _handover()
        with state.lock:
            state.current = self
            pending, state.pending = state.pending, False
        if pending:
            self.start_or_keep_pending()

    def close(self):
        with self.lock:
            self.enabled = False
            self.unloaded = True
            self.cache.clear()
            server, thread = self.server, self.thread
            self.server = self.thread = None
        state = _handover()
        with state.lock:
            if state.current is self:
                state.current = None
        if server is not None:
            server.shutdown()
            server.server_close()
            assert thread is not None
            thread.join(timeout=2)
            arm_handover()


class AliceServer(HTTPServer):
    bridge: AliceBridge

    def handle_error(self, request, client_address):
        pass  # No traceback containing request data or a bearer URL.


class AliceHandler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(1)
        super().setup()

    def log_message(self, format, *args):
        pass  # Never log a bearer URL or request body.

    def send_error(self, code, message=None, explain=None):
        self.respond(405 if code == 501 else code, reply(DENIED))

    def respond(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", ""))
            if self.headers.get("Transfer-Encoding") or length < 0:
                return self.respond(400, reply(DENIED))
            if length > 16384:
                return self.respond(413, reply(DENIED))
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                return self.respond(415, reply(DENIED))
            result = cast(AliceServer, self.server).bridge.handle(self.path, json.loads(self.rfile.read(length)))
        except (ValueError, UnicodeError):
            result = (400, reply(DENIED))
        except TimeoutError:
            result = (408, reply(NOT_SENT))
        except Exception:
            result = (500, reply(NOT_SENT))
        self.respond(*result)
