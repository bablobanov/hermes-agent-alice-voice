"""Real copied SDK + scheduler/dispatch/turn methods and loopback HTTP.
Synthetic fixtures: model worker, session store, transport destination, profile
scope/authorization, runner state and auxiliary turn callbacks; estop is mocked.
No credentials, live gateway, Telegram or Yandex API is used.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
# Optional: caller explicitly selects a reviewed Hermes checkout/runtime.
ENGINE = Path(os.environ.get('ALICE_TEST_ENGINE_ROOT', '.')).resolve()
PYTHON = Path(os.environ.get('ALICE_TEST_ENGINE_PYTHON', sys.executable))
PRISTINE = {
    'hermes_cli/plugins.py': '1b5aae9a34e97c9922b5ab7e76c4bd3562a6ab715a983bf9c4dfe6031c457942',
    'gateway/run_inbound.py': '9f82c79b913e5dd14807b31f04b2a3449b82dc3cdbc7c7779c935bff02c2cc84',
    'gateway/run_turn.py': '56e6fb62b60e954ef0d258c7c44ed3fca99c4aa6d2c502d8f80a1c516569f537',
}


def launch():
    if not os.environ.get('ALICE_TEST_ENGINE_ROOT'):
        raise unittest.SkipTest('optional stock SDK test: set ALICE_TEST_ENGINE_ROOT to a reviewed Hermes tree')
    for relative, expected in PRISTINE.items():
        source = ENGINE / relative
        if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != expected:
            raise unittest.SkipTest('unsupported Hermes tree/version: tested stock SHA mismatch for ' + relative)
    if not PYTHON.is_file():
        raise unittest.SkipTest('optional stock SDK test: selected Python executable is missing')
    with tempfile.TemporaryDirectory(prefix='alice-sdk-') as directory:
        root = Path(directory)
        for name in ('hermes_cli', 'gateway'):
            shutil.copytree(ENGINE / name, root / name, ignore=shutil.ignore_patterns('__pycache__'))
        # Verify CURRENT copied stock sources, never restore or modify the engine.
        for relative, expected in PRISTINE.items():
            if hashlib.sha256((root / relative).read_bytes()).hexdigest() != expected:
                raise AssertionError('current stock source changed during copy: ' + relative)
        home = root / 'home'
        home.mkdir()
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(home), 'HERMES_HOME': str(home / '.hermes'),
               'PYTHONPATH': str(root) + os.pathsep + str(ENGINE), 'PYTHONDONTWRITEBYTECODE': '1'}
        return subprocess.run([str(PYTHON), '-B', str(Path(__file__).resolve()), '--child'],
                              env=env, cwd=root, text=True, capture_output=True, timeout=60)


class IsolatedSDKTests(unittest.TestCase):
    def test_sdk_and_http_real_copied_source(self):
        result = launch()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        print(result.stdout, end='')
        print(result.stderr, end='')


def child():
    import asyncio
    from contextlib import contextmanager
    from contextvars import ContextVar, copy_context
    import http.client
    import secrets
    from threading import Thread, Event
    from types import SimpleNamespace, MethodType
    from gateway.platforms.base import BasePlatformAdapter
    from unittest.mock import patch
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
    from gateway.run_inbound import GatewayInboundMixin
    from gateway.run_turn import GatewayTurnMixin
    from gateway.run_busy import GatewayBusySessionMixin
    from gateway.config import Platform
    from gateway.session import SessionSource
    from agent import estop
    sys.path.insert(0, str(HERE))
    from bridge import AliceBridge
    from test_bridge import envelope, voiced

    session_key = 'agent:main:telegram:dm:1001'
    home = Path(os.environ['HERMES_HOME'])
    home.mkdir(exist_ok=True)
    settings = {'enabled': True, 'skill_id': 'test-skill', 'session_key': session_key,
                'owner_user_id': 'test-account', 'port': 0}
    config = {'plugins': {'entries': {'alice-voice': {'allow_gateway_injection': True, 'settings': settings}}}}
    (home / 'config.yaml').write_text(json.dumps(config))
    os.environ['ALICE_WEBHOOK_SECRET'] = secrets.token_hex(32)

    class SDKTests(unittest.TestCase):
        def setUp(self):
            self.pause = False
            patcher = patch.object(estop, 'is_engaged', lambda: self.pause)
            patcher.start()
            self.addCleanup(patcher.stop)
            self.release = Event()
            self.entered = Event()
            self.calls = []
            self.outputs = []
            self.worker_messages = []
            self.worker_scopes = []
            fixture_scope: ContextVar[tuple[str, str] | None] = ContextVar("synthetic_owner_scope", default=None)
            self.manager = PluginManager(str(home))
            self.ctx = PluginContext(PluginManifest(name='alice-voice'), self.manager)
            owner = self
            source = self.source = SessionSource(platform=Platform.TELEGRAM, chat_id='1001', user_id='1001', chat_type='dm')

            class Runner(GatewayInboundMixin, GatewayTurnMixin, GatewayBusySessionMixin):
                _running = True
                _draining = False
                _MAX_INTERRUPT_DEPTH = 10

                def __init__(self):
                    self._background_tasks = set()
                    self.adapter = SimpleNamespace(_pending_messages={}, _active_sessions={})
                    self.adapter.get_pending_message = MethodType(BasePlatformAdapter.get_pending_message, self.adapter)
                    self.overflow = []
                    self.active = False
                    self.launch_session_id = 'fixture-session'
                    async def lookup(key):
                        return SimpleNamespace(origin=source, session_id='fixture-session') if key == session_key else None
                    self.async_session_store = SimpleNamespace(lookup_by_session_key=lookup)
                    async def handle(event):
                        owner.calls.append(event)
                        if self.active:
                            self._enqueue_fifo(session_key, event, self.adapter)
                            return
                        orphan = self._rescue_orphaned_overflow(session_key, self.adapter)
                        if orphan is not None:
                            self._enqueue_fifo(session_key, event, self.adapter)
                            event = orphan
                        self.active = True
                        try:
                            result = await self._run_agent(message=event.text, context_prompt='', history=[], source=event.source,
                                session_id=self.launch_session_id, session_key=session_key, run_generation=1)
                            owner.outputs.append(result)
                        finally:
                            self.active = False
                    self.adapter.handle_message = handle

                def _is_user_authorized_for_source(self, source, **kwargs):
                    return source.user_id == '1001'
                def _adapter_for_source(self, source):
                    return self.adapter
                @contextmanager
                def _profile_scope_for_source(self, source):
                    # Synthetic ownership marker, not credentials or a profile adapter.
                    token = fixture_scope.set((source.user_id, 'fixture-session'))
                    try:
                        yield
                    finally:
                        fixture_scope.reset(token)
                def _get_proxy_url(self):
                    return None
                def _run_agent_display_settings(self, source):
                    return SimpleNamespace(needs_progress_queue=False, log_mode_enabled=False, _native_slack_task_cards=False)
                def _run_agent_build_turn_context(self, disp, agent_cls, **kw):
                    turn = SimpleNamespace(**kw, result_holder=[None], stream_consumer_holder=[None],
                        streaming_tts_consumer_holder=[None], _status_thread_metadata=None)
                    async def progress():
                        await asyncio.Future()
                    return turn, SimpleNamespace(send_progress_messages=progress, run_sync=lambda: None), self.adapter
                def _run_agent_bind_turn_wiring(self, *args):
                    return None
                def _run_agent_start_streaming_tts(self, *args):
                    return None
                async def _run_agent_stream_consumer_task(self, *args):
                    await asyncio.Future()
                async def _run_agent_track_agent(self, *args):
                    await asyncio.Future()
                async def _run_agent_monitor_for_interrupt(self, *args):
                    await asyncio.Future()
                async def _run_agent_notify_long_running(self, *args):
                    await asyncio.Future()
                def _run_agent_start_turn_worker(self, turn, run_sync):
                    owner.worker_messages.append(turn.message)
                    def model_fixture():
                        owner.worker_scopes.append(fixture_scope.get())
                        owner.entered.set()
                        if not owner.release.wait(5):
                            raise RuntimeError('fixture release timeout')
                        return {'final_response': 'SYNTHETIC fixture reply: ' + turn.message,
                                'completed': True, 'messages': []}
                    return SimpleNamespace(executor_task=asyncio.get_running_loop().run_in_executor(None, copy_context().run, model_fixture))
                async def _run_agent_await_turn_worker(self, worker, turn, *args):
                    turn.result_holder[0] = await worker.executor_task
                    return turn.result_holder[0]
                def _run_agent_evict_on_fallback(self, *args):
                    pass
                async def _run_agent_finalize_streaming_tts(self, *args):
                    pass
                def _pending_event_audio_paths(self, event):
                    return []
                def _peek_session_state(self, key):
                    return self._session_state(key)
                def _session_state(self, key):
                    return SimpleNamespace(conversation=SimpleNamespace(queued_events=self.overflow))
                async def _run_agent_cleanup_turn_tasks(self, turn, **tasks):
                    for task in tasks.values():
                        if task:
                            task.cancel()
                    await asyncio.gather(*(t for t in tasks.values() if t), return_exceptions=True)
                async def _run_agent_mark_streamed_delivery(self, *args):
                    pass
                def _run_agent_schedule_bubble_cleanup(self, *args):
                    pass
                def _is_goal_continuation_event(self, event):
                    return False
                def _session_key_for_source(self, source):
                    return session_key
                async def _prepare_profile_scoped_inbound_message_text(self, *, event, **kwargs):
                    return event.text
                def _reply_anchor_for_event(self, event):
                    return None
                async def _refresh_agent_cache_message_count(self, *args):
                    pass
                async def _run_agent_deliver_first_response(self, turn, adapter, response, result, stream_task):
                    owner.outputs.append(response)

            self.runner = Runner()
            self.loop = asyncio.new_event_loop()
            self.runner._gateway_loop = self.loop
            self.thread = Thread(target=self.loop.run_forever, daemon=True)
            self.thread.start()
            self.manager.set_gateway_message_injector(self.runner, self.runner._schedule_plugin_message_injection)
            self.addCleanup(self.stop)

        def stop(self):
            self.release.set()
            future = asyncio.run_coroutine_threadsafe(self.wait_background(), self.loop)
            future.result(10)
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(3)
            self.loop.close()

        async def wait_background(self):
            # Cross-thread scheduled dispatch futures aren't in _background_tasks.
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()
                     and getattr(t.get_coro(), '__name__', '') == '_dispatch_plugin_message_injection']
            if tasks:
                await asyncio.gather(*tasks)  # Unexpected routing/turn exceptions must fail the probe.

        def inject(self, text='literal fixture', **extra):
            return self.ctx.inject_message(text, role='user', session_key=session_key, **extra)

        def wait_done(self):
            asyncio.run_coroutine_threadsafe(self.wait_background(), self.loop).result(10)

        def test_pristine_imports_and_stock_signature(self):
            import inspect
            import hermes_cli.plugins as sdk
            import gateway.run_inbound as inbound
            import gateway.run_turn as turn
            for module, relative in ((sdk, 'hermes_cli/plugins.py'),
                                     (inbound, 'gateway/run_inbound.py'),
                                     (turn, 'gateway/run_turn.py')):
                self.assertIsNotNone(module.__file__)
                path = Path(module.__file__ or "").resolve()
                self.assertTrue(path.is_relative_to(Path.cwd()))
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), PRISTINE[relative])
            self.assertEqual(list(inspect.signature(self.ctx.inject_message).parameters),
                             ['content', 'role', 'session_key'])
            self.assertFalse(any('injection_patch' in name for name in sys.modules))

        def post(self, bridge, text, message_id):
            connection = http.client.HTTPConnection(*bridge.server.server_address, timeout=2)
            try:
                connection.request('POST', '/alice/' + os.environ['ALICE_WEBHOOK_SECRET'],
                    body=json.dumps(envelope(text, message_id)), headers={'Content-Type': 'application/json'})
                response = connection.getresponse()
                status = response.status
                return status, json.loads(response.read())['response']
            finally:
                connection.close()

        def http_bridge(self):
            bridge = AliceBridge(self.ctx, paused=lambda: self.pause)
            self.assertTrue(bridge.enabled)
            bridge.on_telegram_connect(None, None)
            self.addCleanup(bridge.close)
            return bridge

        def test_real_http_ack_stock_dispatch_and_telegram_fixture_output(self):
            bridge = self.http_bridge()
            status, ack = self.post(bridge, 'literal fixture HTTP', 1)
            self.assertEqual(status, 200)
            self.assertEqual(ack['text'], 'Запрос принят для обработки. Ответ будет в Telegram')
            self.assertTrue(self.entered.wait(3))
            self.assertEqual(self.outputs, [], 'ACK must precede synthetic worker completion')
            self.assertEqual(self.post(bridge, 'literal fixture HTTP', 1), (status, ack))
            for phrase in ('Ответ готов?', 'Дальше'):
                self.assertEqual(self.post(bridge, phrase, 2)[1]['text'], 'Ответ будет в Telegram')
            self.assertEqual(len(self.calls), 1)
            event = self.calls[0]
            self.assertTrue(event.internal)
            self.assertFalse(event.allow_gateway_control)
            self.assertEqual(event.text, voiced('literal fixture HTTP'))
            self.assertEqual(event.source.chat_id, '1001')
            self.assertEqual(event.metadata['gateway_session_key'], session_key)
            self.assertEqual(event.metadata['gateway_session_id'], 'fixture-session')
            self.assertTrue(event.metadata['gateway_session_strict'])
            self.assertEqual(event.metadata['hermes_plugin_id'], 'alice-voice')
            self.release.set()
            self.wait_done()
            self.assertEqual(self.outputs[0]['final_response'],
                             'SYNTHETIC fixture reply: ' + voiced('literal fixture HTTP'))
            self.assertEqual(self.post(bridge, 'Ответ готов?', 3)[1]['text'], 'Ответ будет в Telegram')
            self.assertEqual(len(self.calls), 1)
            print('SYNTHETIC: real stock HTTP/SDK dispatch/turn, ACK before worker; no voice result, no live delivery')

        def test_stock_fifo_keeps_event_text_not_correlated_results(self):
            self.assertTrue(self.inject('first'))
            self.assertTrue(self.entered.wait(3))
            self.assertTrue(self.inject('second'))
            self.assertTrue(self.inject('third'))
            # Barrier only waits for queued dispatch admission, not turn completion.
            asyncio.run_coroutine_threadsafe(asyncio.sleep(.05), self.loop).result(2)
            self.assertEqual(len(self.calls), 3)
            self.release.set()
            self.wait_done()
            self.assertEqual(self.worker_messages, ['first', 'second', 'third'])
            self.assertEqual(self.worker_scopes, [('1001', 'fixture-session')] * 3)
            self.assertEqual(len(self.outputs), 3)
            print('SYNTHETIC: stock FIFO drains three fixture events; no receipt/result association is asserted')

        def test_stock_orphan_rescue_preserves_event_ownership(self):
            self.runner.active = True
            self.assertTrue(self.inject('orphan one'))
            self.assertTrue(self.inject('orphan two'))
            self.wait_done()
            slot = self.runner.adapter._pending_messages.pop(session_key)
            self.runner.overflow.insert(0, slot)
            self.runner.active = False
            self.release.set()
            self.assertTrue(self.inject('new event'))
            self.wait_done()
            self.assertEqual(self.worker_messages, ['orphan one', 'orphan two', 'new event'])

        def test_capability_gate_refuses_scheduling_without_ack(self):
            bridge = self.http_bridge()
            config['plugins']['entries']['alice-voice']['allow_gateway_injection'] = False
            (home / 'config.yaml').write_text(json.dumps(config))
            try:
                status, response = self.post(bridge, 'denied fixture', 1)
                self.assertEqual(status, 503)
                self.assertIn('не отправлен', response['text'])
                self.assertEqual(self.calls, [])
            finally:
                config['plugins']['entries']['alice-voice']['allow_gateway_injection'] = True
                (home / 'config.yaml').write_text(json.dumps(config))

        def test_no_gateway_cannot_ack(self):
            self.runner._running = False
            bridge = self.http_bridge()
            self.assertEqual(self.post(bridge, 'fixture', 1)[0], 503)
            self.assertEqual(self.calls, [])

        def test_scheduled_is_not_routed_or_delivered(self):
            # Missing session and unauthorized session are async dispatch failures,
            # NOT admission failures: stock True cannot be represented as delivery.
            self.release.set()
            for authorized in (False, True):
                with self.subTest(authorized=authorized):
                    async def lookup(key):
                        return SimpleNamespace(origin=self.source, session_id='fixture-session') if not authorized else None
                    self.runner.async_session_store.lookup_by_session_key = lookup
                    self.runner._is_user_authorized_for_source = lambda source, **kw: authorized
                    self.assertTrue(self.inject('async refusal fixture'))
                    self.wait_done()
            self.assertEqual(self.worker_messages, [])
            self.assertEqual(self.outputs, [])

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(SDKTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    if '--child' in sys.argv:
        sys.exit(child())
    unittest.main(verbosity=2)
