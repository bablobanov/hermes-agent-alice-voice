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


# A plugin reload in a running gateway (reload-plugins: `hermes plugins enable/disable`, the Desktop
# switch) unloads this plugin, which closes the listener, and registers it again. The engine does not
# call the new connect factory on the same Telegram client: it keys factories by plugin and
# __qualname__ and skips a known key. So the unload leaves a marker, and the next registration in the
# same process takes the listener over. The marker lives in sys.modules because the engine imports the
# plugin package again on a forced re-discovery, which would reset a module global.
_HANDOVER = "_alice_voice_listener_handover"


def _handover():
    module = sys.modules.get(_HANDOVER)
    if module is None:
        fresh = types.ModuleType(_HANDOVER, "alice-voice: a listener closed by a plugin unload")
        fresh.lock, fresh.pending = Lock(), False
        module = sys.modules.setdefault(_HANDOVER, fresh)
    return module


def take_handover():
    """True once after an unload closed a serving listener in this process."""
    state = _handover()
    with state.lock:
        pending, state.pending = state.pending, False
    return pending


class AliceBridge:
    def __init__(self, ctx, paused=None):
        self.ctx = ctx
        self.cache = OrderedDict()
        self.lock = RLock()
        self.server = self.thread = None
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
        self.start()

    def start(self):
        with self.lock:
            if not self.enabled or self.server is not None:
                return
            self.server = AliceServer(("127.0.0.1", self.port), AliceHandler)
            self.server.bridge = self
            self.thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1},
                                 name="alice-voice", daemon=True)
            self.thread.start()

    def take_over_after_reload(self):
        """In register(): listen now if an unload in this process closed a serving listener."""
        if not self.enabled or not take_handover():
            return
        try:
            self.start()
        except OSError as exc:  # the port is taken: a later Telegram connect still binds
            logger.warning("alice-voice: listener not restored after the plugin reload (%s)", type(exc).__name__)

    def close(self):
        with self.lock:
            self.enabled = False
            self.cache.clear()
            server, thread = self.server, self.thread
            self.server = self.thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
            assert thread is not None
            thread.join(timeout=2)
            state = _handover()
            with state.lock:
                state.pending = True


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
