"""Local-only unittest checks; never connect to Telegram or Yandex."""
import importlib.util
from contextlib import redirect_stderr
import http.client
import io
import json
import os
from pathlib import Path
import secrets
import sys
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
SESSION = "agent:main:telegram:dm:1001"  # Synthetic destination, never a real chat.
ACK = "Запрос принят для обработки. Ответ будет в Telegram"


class Context:
    profile_name = "default"

    def __init__(self, **settings):
        self.settings = {"enabled": True, "skill_id": "test-skill", "session_key": SESSION,
                         "port": 0, **settings}
        self.calls = []
        self.factories = []
        self.cleanup = []
        self.accept = True

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def inject_message(self, content, role="user", *, session_key=None):
        self.calls.append((content, role, session_key))
        if isinstance(self.accept, Exception):
            raise self.accept
        return self.accept

    def register_telegram_handler(self, factory):
        self.factories.append(factory)

    def on_unload(self, callback):
        self.cleanup.append(callback)
        return object()  # the engine's registration handle; None after an abandoned load


class AbandonedLoadContext(Context):
    """A load that crosses plugins.load_timeout_seconds: from the deadline on, every registrar is a no-op
    returning None (hermes_cli/plugins.py _ignore_after_abandoned_load, 0.22.2 and 0.23.0). Here the
    deadline lands right before on_unload, so a factory registered after it would still be recorded:
    the plugin must not register one."""

    def on_unload(self, callback):
        return None


def free_port():
    import socket
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def listening(port):
    import socket
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def envelope(text="Сохрани заметку: Xiaomi 14T Pro", message_id=1):
    return {"version": "1.0", "request": {"type": "SimpleUtterance",
            "command": text.lower(), "original_utterance": text},
            "session": {"skill_id": "test-skill", "session_id": "test-session",
                        "message_id": message_id, "new": False,
                        "user": {"user_id": "test-account"}}}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.secret = secrets.token_hex(32)  # Ephemeral test bearer, not a live credential.
        self.env = patch.dict(os.environ, {"ALICE_WEBHOOK_SECRET": self.secret})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.path = "/alice/" + self.secret
        # Process-wide state that outlives a plugin reload (bridge.py): every test starts as a fresh process.
        sys.modules.pop("_alice_voice_listener_handover", None)
        self.addCleanup(sys.modules.pop, "_alice_voice_listener_handover", None)

    def bridge(self, ctx=None, paused=lambda: False):
        if not (ROOT / "bridge.py").exists():
            self.fail("Alice bridge is not implemented")
        spec = importlib.util.spec_from_file_location("alice_test_bridge", ROOT / "bridge.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.AliceBridge(ctx or Context(), paused=paused)
        self.addCleanup(result.close)
        return result

    def test_happy_path_preserves_text_and_fixed_destination(self):
        bridge = self.bridge()
        text = '  Запиши "Xiaomi 14T Pro": Gemini по умолчанию  '
        data = envelope(text)
        data.update(session_key="attacker", role="system", model="attacker", tools=["approve"])
        data["request"]["origin"] = "attacker"
        status, reply = bridge.handle(self.path, data)
        self.assertEqual(status, 200)
        self.assertEqual(reply, {"version": "1.0", "response": {"text": ACK, "end_session": False}})
        self.assertEqual(bridge.ctx.calls, [(text, "user", SESSION)])
    def test_secret_skill_and_optional_account_binding(self):
        for changed in ("secret", "skill", "account", "anonymous"):
            with self.subTest(changed=changed):
                bridge = self.bridge(Context(owner_user_id="test-account"))
                data = envelope()
                path = self.path
                if changed == "secret":
                    path += "/extra"
                elif changed == "skill":
                    data["session"]["skill_id"] = "other-skill"
                elif changed == "account":
                    data["session"]["user"]["user_id"] = "other-account"
                else:
                    del data["session"]["user"]
                    data["session"]["user_id"] = "test-account"  # Legacy/body id is no auth.
                self.assertEqual(bridge.handle(path, data)[0], 403)
                self.assertEqual(bridge.ctx.calls, [])
        bridge = self.bridge()  # Without owner binding, private skill + bearer is the principal.
        data = envelope()
        data["session"]["user"]["user_id"] = "another-signed-in-account"
        self.assertEqual(bridge.handle(self.path, data)[0], 200)

    def test_standard_envelope_and_text_only_gate(self):
        bridge = self.bridge()
        data = envelope()
        data.update(meta={"locale": "ru-RU", "timezone": "Europe/Moscow", "interfaces": {}},
                    state={"session": {"session_key": "attacker"}, "user": {}, "application": {}})
        data["session"]["application"] = {"application_id": "test-app"}
        data["request"]["nlu"] = {"tokens": [], "entities": [], "intents": {}}
        self.assertEqual(bridge.handle(self.path, data)[0], 200)
        invalid = [("version", "2.0"), ("request.type", "ButtonPressed"),
                   ("request.command", 1), ("request.original_utterance", ["text"]),
                   ("request.original_utterance", "x" * 4097),
                   ("request.command", "/approve"), ("request.original_utterance", " /stop"),
                   ("request.original_utterance", "task\u0000"), ("session", []),
                   ("session.session_id", 123), ("session.message_id", True),
                   ("session.user", "test-account")]
        for field, value in invalid:
            with self.subTest(field=field):
                bridge = self.bridge()
                data = envelope()
                target = data
                parts = field.split(".")
                for part in parts[:-1]:
                    target = target[part]
                target[parts[-1]] = value
                self.assertIn(bridge.handle(self.path, data)[0], (400, 403))
                self.assertEqual(bridge.ctx.calls, [])

    def test_inactive_configuration_and_core_pause_do_not_inject(self):
        cases = [{"enabled": False}, {"enabled": "true"}, {"skill_id": ""},
                 {"session_key": ""}, {"port": "8788"}]
        for settings in cases:
            with self.subTest(settings=settings):
                bridge = self.bridge(Context(**settings))
                self.assertEqual(bridge.handle(self.path, envelope())[0], 503)
                self.assertEqual(bridge.ctx.calls, [])
        ctx = Context()
        ctx.settings.pop("enabled")
        bridge = self.bridge(ctx)
        self.assertEqual(bridge.handle(self.path, envelope())[0], 503)
        ctx = Context()
        ctx.profile_name = "other-profile"
        bridge = self.bridge(ctx)
        self.assertEqual(bridge.handle(self.path, envelope())[0], 503)
        for secret in ("", "short", "<вставь_сюда>", "0" * 64):
            with patch.dict(os.environ, {"ALICE_WEBHOOK_SECRET": secret}):
                bridge = self.bridge()
                self.assertEqual(bridge.handle("/alice/" + secret, envelope())[0], 503)
                self.assertEqual(bridge.ctx.calls, [])
        bridge = self.bridge(paused=lambda: True)
        self.assertEqual(bridge.handle(self.path, envelope())[0], 503)
        self.assertEqual(bridge.ctx.calls, [])
        # The default predicate calls the SDK's estop API; no live sentinel is changed.
        from types import ModuleType
        agent, estop = ModuleType("agent"), ModuleType("agent.estop")
        observed = []
        setattr(estop, "is_engaged", lambda: observed.append(True) or True)
        with patch.dict(sys.modules, {"agent": agent, "agent.estop": estop}):
            bridge = self.bridge(paused=None)
            self.assertEqual(bridge.handle(self.path, envelope())[0], 503)
        self.assertEqual(observed, [True])
        self.assertEqual(bridge.ctx.calls, [])

    def test_duplicate_retry_cache_is_small_and_memory_only(self):
        bridge = self.bridge()
        data = envelope()
        first = bridge.handle(self.path, data)
        self.assertEqual(bridge.handle(self.path, data), first)
        self.assertEqual(len(bridge.ctx.calls), 1)
        for message_id in range(2, 35):
            self.assertEqual(bridge.handle(self.path, envelope(message_id=message_id))[0], 200)
        before = len(bridge.ctx.calls)
        self.assertEqual(bridge.handle(self.path, data)[0], 200)
        self.assertEqual(len(bridge.ctx.calls), before + 1)  # Old entry was evicted.
        data["session"]["user"]["user_id"] = "second-account"
        self.assertEqual(bridge.handle(self.path, data)[0], 200)
        self.assertEqual(len(bridge.ctx.calls), before + 2)

    def test_not_scheduled_is_not_acknowledged_or_cached(self):
        for outcome in (False, RuntimeError("untrusted body/path")):
            with self.subTest(outcome=type(outcome).__name__):
                bridge = self.bridge()
                bridge.ctx.accept = outcome
                status, response = bridge.handle(self.path, envelope())
                self.assertEqual(status, 503)
                self.assertIn("не отправлен", response["response"]["text"])
                self.assertNotIn("untrusted", str(response))
                bridge.ctx.accept = True
                self.assertEqual(bridge.handle(self.path, envelope())[0], 200)
                self.assertEqual(len(bridge.ctx.calls), 2)
                self.assertEqual(bridge.handle(self.path, envelope())[0], 200)
                self.assertEqual(len(bridge.ctx.calls), 2)

    def test_activation_help_exit_and_command_fallback(self):
        for command in ("", "помощь", "выход"):
            with self.subTest(command=command):
                bridge = self.bridge()
                data = envelope(command)
                if not command:
                    data["session"]["new"] = True
                    data["request"]["original_utterance"] = "Запусти навык Гермес"
                status, response = bridge.handle(self.path, data)
                self.assertEqual(status, 200)
                self.assertNotEqual(response["response"]["text"], ACK)
                self.assertEqual(response["response"]["end_session"], command == "выход")
                self.assertEqual(bridge.ctx.calls, [])
        bridge = self.bridge()
        data = envelope("Задача без original")
        data["request"]["original_utterance"] = ""
        self.assertEqual(bridge.handle(self.path, data)[0], 200)
        self.assertEqual(bridge.ctx.calls, [(data["request"]["command"], "user", SESSION)])

    def test_plugin_starts_only_on_telegram_connect_and_unloads(self):
        if not (ROOT / "__init__.py").exists():
            self.fail("Plugin entry point is not implemented")
        spec = importlib.util.spec_from_file_location("alice_test_plugin", ROOT / "__init__.py",
                                                     submodule_search_locations=[str(ROOT)])
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(module)
        for ctx in (Context(enabled=False), Context(skill_id="")):
            module.register(ctx)
            self.assertEqual(ctx.factories, [])
            self.assertEqual(ctx.cleanup, [])
        ctx = Context()
        ctx.profile_name = "other-profile"
        module.register(ctx)
        self.assertEqual(ctx.factories, [])
        ctx = Context()
        module.register(ctx)
        self.assertEqual(len(ctx.factories), 1)
        self.assertEqual(len(ctx.cleanup), 1)
        self.addCleanup(ctx.cleanup[0])
        bridge = ctx.factories[0].__self__
        self.assertIsNone(bridge.server)  # Import and register do not bind a port.
        self.assertIsNone(ctx.factories[0](None, object()))
        server, thread = bridge.server, bridge.thread
        self.assertEqual(server.server_address[0], "127.0.0.1")
        self.assertIsNone(ctx.factories[0](None, object()))
        self.assertIs(bridge.server, server)  # Reconnect does not bind twice.
        ctx.cleanup[0]()
        self.assertFalse(thread.is_alive())
        self.assertEqual(server.socket.fileno(), -1)
        ctx.factories[0](None, object())
        self.assertIsNone(bridge.server)
        self.assertEqual(ctx.calls, [])

    def load_plugin(self, name):
        """A fresh copy of the plugin package, as the engine imports it again on a forced re-discovery."""
        spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py",
                                                     submodule_search_locations=[str(ROOT)])
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        self.addCleanup(sys.modules.pop, spec.name, None)
        self.addCleanup(sys.modules.pop, spec.name + ".bridge", None)
        spec.loader.exec_module(module)
        return module

    def post(self, port, text, message_id):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            connection.request("POST", self.path, body=json.dumps(envelope(text, message_id)).encode(),
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())["response"]["text"]
        finally:
            connection.close()

    def test_listener_comes_back_after_a_plugin_reload(self):
        """`hermes plugins enable/disable` of any plugin, or the Desktop switch, sends a running gateway
        `reload-plugins`: a forced re-discovery unloads every plugin and registers it again. The engine
        keys a connect factory by (plugin, __qualname__) and skips a known key on the same Telegram
        client, so the new AliceBridge.on_telegram_connect is never called (Hermes 0.22.2 and 0.23.0,
        gateway/platforms/base.py _wire_plugin_handlers), while the unload closed the old listener.
        The new registration must take the fixed port over itself; requests then reach the new
        context, never the unloaded one."""
        port = free_port()
        old_ctx = Context(port=port)
        self.load_plugin("alice_reload_before").register(old_ctx)
        old_ctx.factories[0](None, object())  # the gateway's Telegram connect
        self.assertEqual(self.post(port, "before the reload", 1), (200, ACK))
        for callback in old_ctx.cleanup:  # the forced re-discovery unloads ...
            callback()
        new_ctx = Context(port=port)
        self.load_plugin("alice_reload_after").register(new_ctx)  # ... and registers again
        self.addCleanup(lambda: [callback() for callback in new_ctx.cleanup])
        # The engine does not call new_ctx.factories[0]: same plugin, same qualname, same client.
        self.assertEqual(self.post(port, "after the reload", 2), (200, ACK))
        self.assertEqual([call[0] for call in old_ctx.calls], ["before the reload"])
        self.assertEqual([call[0] for call in new_ctx.calls], ["after the reload"])
        bridge = new_ctx.factories[0].__self__
        server = bridge.server
        new_ctx.factories[0](None, object())  # a later Telegram reconnect: no second bind
        self.assertIs(bridge.server, server)

    def serve_then_unload(self, port):
        ctx = Context(port=port)
        self.load_plugin("alice_serving").register(ctx)
        ctx.factories[0](None, object())
        for callback in ctx.cleanup:
            callback()
        return ctx

    def test_abandoned_load_never_takes_the_listener_over(self):
        """A reload whose load timed out: no unload callback could ever close a listener started there.
        The next registration still listens."""
        port = free_port()
        self.serve_then_unload(port)
        abandoned = AbandonedLoadContext(port=port)
        self.load_plugin("alice_abandoned").register(abandoned)
        self.assertEqual(abandoned.factories, [])  # no factory without its cleanup
        self.assertFalse(listening(port))
        ctx = Context(port=port)
        self.load_plugin("alice_next").register(ctx)
        self.addCleanup(lambda: [callback() for callback in ctx.cleanup])
        self.assertTrue(listening(port))

    def test_failed_bind_after_a_reload_is_retried_by_the_next_one(self):
        port = free_port()
        self.serve_then_unload(port)
        busy = Context(port=port)
        module = self.load_plugin("alice_busy")

        def taken(*args, **kwargs):
            raise OSError("address in use (simulated)")

        bridge_module = sys.modules["alice_busy.bridge"]
        real_server, bridge_module.AliceServer = bridge_module.AliceServer, taken
        try:
            with self.assertLogs("alice-voice", "WARNING"):
                module.register(busy)
        finally:
            bridge_module.AliceServer = real_server
        self.assertIsNone(busy.factories[0].__self__.server)
        for callback in busy.cleanup:
            callback()
        retry = Context(port=port)
        self.load_plugin("alice_retry").register(retry)
        self.addCleanup(lambda: [callback() for callback in retry.cleanup])
        self.assertTrue(listening(port))

    def test_late_connect_of_an_unloaded_bridge_starts_its_successor(self):
        """The engine's connect or re-wire took its factory list before a reload unloaded the bridge,
        and calls the old factory afterwards; it then marks the key wired, so the successor's factory
        never runs. The connect still grants the right to listen: the successor gets it."""
        port = free_port()
        old = Context(port=port)
        self.load_plugin("alice_late_old").register(old)
        late_factory = old.factories[0]  # taken before the unload, never called before it
        for callback in old.cleanup:
            callback()
        new = Context(port=port)
        self.load_plugin("alice_late_new").register(new)
        self.addCleanup(lambda: [callback() for callback in new.cleanup])
        self.assertFalse(listening(port))  # no Telegram connect in this process yet
        late_factory(None, object())
        self.assertTrue(listening(port))
        self.assertIsNone(late_factory.__self__.server)  # the unloaded bridge itself never binds
        self.assertEqual(self.post(port, "after the late connect", 1), (200, ACK))
        self.assertEqual([call[0] for call in new.calls], ["after the late connect"])

    def test_successor_unloaded_between_handover_and_start_the_next_registration_listens(self):
        """A late connect picks the registered successor; another reload unloads that successor before
        it starts. The next registration must listen."""
        port = free_port()
        old = Context(port=port)
        self.load_plugin("alice_race_old").register(old)
        late_factory = old.factories[0]
        for callback in old.cleanup:
            callback()
        successor = Context(port=port)
        self.load_plugin("alice_race_successor").register(successor)
        bridge = successor.factories[0].__self__
        real_start = bridge.start

        def unloaded_first():  # the barrier: the successor's unload lands right before its start
            for callback in successor.cleanup:
                callback()
            return real_start()

        bridge.start = unloaded_first
        late_factory(None, object())
        self.assertFalse(listening(port))
        third = Context(port=port)
        self.load_plugin("alice_race_third").register(third)
        self.addCleanup(lambda: [callback() for callback in third.cleanup])
        self.assertTrue(listening(port))

    def test_successor_registered_while_a_late_connect_is_in_flight_listens(self):
        """A late connect picks the registered bridge A; before A starts, another reload unloads A and
        registers B all the way through. B must end up listening, without yet another registration."""
        port = free_port()
        old = Context(port=port)
        self.load_plugin("alice_flight_old").register(old)
        late_factory = old.factories[0]
        for callback in old.cleanup:
            callback()
        first = Context(port=port)
        self.load_plugin("alice_flight_a").register(first)
        bridge = first.factories[0].__self__
        real_start = bridge.start
        second = Context(port=port)

        def reload_in_between():  # the barrier: A unloaded and B registered before A starts
            for callback in first.cleanup:
                callback()
            self.load_plugin("alice_flight_b").register(second)
            return real_start()

        bridge.start = reload_in_between
        self.addCleanup(lambda: [callback() for callback in second.cleanup])
        late_factory(None, object())
        self.assertTrue(listening(port))
        self.assertEqual(self.post(port, "after the race", 1), (200, ACK))
        self.assertEqual([call[0] for call in second.calls], ["after the race"])

    def test_late_connect_before_the_successor_registers_lets_it_listen(self):
        port = free_port()
        old = Context(port=port)
        self.load_plugin("alice_early_old").register(old)
        for callback in old.cleanup:
            callback()
        old.factories[0](None, object())  # between the unload and the next registration
        self.assertFalse(listening(port))
        new = Context(port=port)
        self.load_plugin("alice_early_new").register(new)
        self.addCleanup(lambda: [callback() for callback in new.cleanup])
        self.assertTrue(listening(port))

    def test_disabled_or_foreign_registration_keeps_the_right_and_binds_nothing(self):
        """A reload that registers the plugin disabled in its settings, or in a non-default profile,
        binds nothing; enabling it again in the default profile listens at once."""
        port = free_port()
        self.serve_then_unload(port)
        foreign = Context(port=port)
        foreign.profile_name = "other-profile"
        for ctx, name in ((Context(port=port, enabled=False), "alice_disabled"), (foreign, "alice_foreign")):
            self.load_plugin(name).register(ctx)
            self.assertEqual(ctx.factories, [])
            self.assertFalse(listening(port))
        again = Context(port=port)
        self.load_plugin("alice_enabled_again").register(again)
        self.addCleanup(lambda: [callback() for callback in again.cleanup])
        self.assertTrue(listening(port))

    def test_reload_without_a_serving_listener_binds_nothing(self):
        """A registration that follows no serving listener (a CLI process, or a gateway whose Telegram
        never connected) still binds only on the connect factory."""
        ctx = Context(port=free_port())
        self.load_plugin("alice_first_load").register(ctx)
        self.addCleanup(lambda: [callback() for callback in ctx.cleanup])
        self.assertIsNone(ctx.factories[0].__self__.server)
        for callback in ctx.cleanup:  # unload before any connect
            callback()
        again = Context(port=ctx.settings["port"])
        self.load_plugin("alice_second_load").register(again)
        self.addCleanup(lambda: [callback() for callback in again.cleanup])
        self.assertIsNone(again.factories[0].__self__.server)


    def test_close_releases_lock_before_shutdown(self):
        bridge = self.bridge()
        self.assertEqual(bridge.handle(self.path, envelope())[0], 200)
        owner = self
        probes = []

        class Server:
            def shutdown(self):
                finished = Event()

                def pending_handler():
                    probes.append(bridge.handle(owner.path, envelope())[0])
                    finished.set()

                worker = Thread(target=pending_handler, daemon=True)
                worker.start()
                try:
                    owner.assertTrue(finished.wait(1), "shutdown must not hold bridge.lock")
                    owner.assertFalse(bridge.enabled)
                    owner.assertEqual(bridge.cache, {})
                finally:
                    worker.join(timeout=1)

            def server_close(self):
                probes.append("closed")

        class ListenerThread:
            def join(self, timeout):
                probes.append(("joined", timeout))

        bridge.server, bridge.thread = Server(), ListenerThread()
        bridge.close()
        bridge.close()  # Unload cleanup is idempotent.
        self.assertEqual(probes, [503, "closed", ("joined", 2)])
        self.assertIsNone(bridge.server)
        self.assertIsNone(bridge.thread)
        self.assertEqual(len(bridge.ctx.calls), 1)

    def test_real_loopback_post_short_ack_and_no_path_logging(self):
        bridge = self.bridge()
        bridge.on_telegram_connect(None, object())
        connection = http.client.HTTPConnection(*bridge.server.server_address, timeout=2)
        self.addCleanup(connection.close)
        captured = io.StringIO()
        started = time.monotonic()
        with redirect_stderr(captured):
            for path in (self.path, self.path, "/alice/wrong"):
                connection.request("POST", path, body=json.dumps(envelope()).encode(),
                                   headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                raw = response.read()
                self.assertEqual(response.status, 403 if path != self.path else 200)
                data = json.loads(raw)
                if path == self.path:
                    self.assertEqual(data["response"]["text"], ACK)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(bridge.ctx.calls, [(envelope()["request"]["original_utterance"], "user", SESSION)])
        self.assertEqual(captured.getvalue(), "")

    def test_http_ingress_method_body_limit_and_json(self):
        bridge = self.bridge()
        bridge.on_telegram_connect(None, object())
        captured = io.StringIO()
        for method, body, expected in (("GET", b"", 405),
                                       ("POST", b"x" * 16385, 413),
                                       ("POST", b"{", 400)):
            with self.subTest(expected=expected), redirect_stderr(captured):
                connection = http.client.HTTPConnection(*bridge.server.server_address, timeout=2)
                try:
                    connection.request(method, self.path, body=body,
                                       headers={"Content-Type": "application/json"})
                    response = connection.getresponse()
                    raw = response.read()
                    self.assertEqual(response.status, expected)
                    self.assertEqual(json.loads(raw)["version"], "1.0")
                finally:
                    connection.close()
        self.assertEqual(bridge.ctx.calls, [])
        self.assertEqual(captured.getvalue(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
