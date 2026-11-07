# Alice voice input for Hermes

![Alice sends a voice task to Hermes: a violet holographic assistant and the Hermes robot reach toward a visible audio waveform](assets/hero.png)

[Русский](README.ru.md) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md)

**0.3.0 — voice input only, without Hermes SDK patches.** A private Yandex Alice skill sends recognized text to one existing Hermes Telegram DM session. Hermes processes the task through its normal gateway; the result is delivered in Telegram, not spoken by Alice.

```text
Alice → private skill → HTTPS endpoint → local plugin → stock gateway injection → Telegram
```

Alice says “Запрос принят для обработки. Ответ будет в Telegram”. This acknowledges **asynchronous scheduling only**, not successful routing, model completion, or Telegram delivery. “Ответ готов?” and “Дальше” only remind you to check Telegram; there is no result reader, completion callback, or Alice TTS.

## Status and compatibility

One operator reported a successful real voice → private Alice skill → Hermes → Telegram chain. This is not verification of every live authentication, retry, network, or model case.

The manifest declares `requires_hermes: ">=0.21.3"` as installation metadata, **not a universally verified compatibility floor**. Tested compatibility is with the concrete stock sources below, using the existing `PluginContext.inject_message(content, role="user", session_key=...)`, Telegram connect factory, unload callback, and core estop API. Other revisions need review/testing; no core changes or patch scripts are supplied.

| Tested current stock file | SHA-256 |
| --- | --- |
| `hermes_cli/plugins.py` | `1b5aae9a34e97c9922b5ab7e76c4bd3562a6ab715a983bf9c4dfe6031c457942` |
| `gateway/run_inbound.py` | `9f82c79b913e5dd14807b31f04b2a3449b82dc3cdbc7c7779c935bff02c2cc84` |
| `gateway/run_turn.py` | `56e6fb62b60e954ef0d258c7c44ed3fca99c4aa6d2c502d8f80a1c516569f537` |

## Install

Prerequisites: a working Hermes Telegram gateway in the **default profile**, an already-created and authorized Telegram DM session, a private Alice skill, and an HTTPS reverse proxy you control. The plugin uses Python's standard library; use the Python version supported by your Hermes installation. Consult the current [Hermes plugin docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins) for your installation's trust and permissions model.

1. Review `plugins/alice-voice/__init__.py`, `bridge.py`, and `plugin.yaml`. Copy these three files into the `alice-voice` directory of your existing Hermes plugin folder (normally `$HERMES_HOME/plugins`, or `~/.hermes/plugins` for the default profile). Tests need not be installed. Use your normal account and plugin review process; do not bypass your installation's permission controls.
2. Generate a unique 32-byte random secret locally, for example `python3 -c 'import secrets; print(secrets.token_hex(32))'`. In the Hermes `.env` file shown by `hermes config env-path`, set `ALICE_WEBHOOK_SECRET=<YOUR_64_HEX_RANDOM_SECRET>`. Replace the placeholder with your generated value; do not commit it or place credentials in an Alice request payload. Protect that file and restrict access to your skill configuration.
3. Merge this **placeholder-only** example into your Hermes `config.yaml` without overwriting other plugins. Replace every placeholder with your own value:

```yaml
plugins:
  enabled:
    - alice-voice
  entries:
    alice-voice:
      allow_gateway_injection: true
      settings:
        enabled: true
        skill_id: "<YOUR_PRIVATE_ALICE_SKILL_ID>"
        session_key: "agent:main:telegram:dm:<YOUR_TELEGRAM_USER_ID>"
        owner_user_id: "<YOUR_ALICE_ACCOUNT_USER_ID>"
        port: 8788
```

`session_key` must name an existing authorized Telegram DM with a positive numeric user ID. It is fixed by operator configuration, never selected by the payload. `owner_user_id` is optional: use `""` only if you deliberately accept any signed-in account presenting the bearer and matching skill ID. Placeholder skill/secret/session values will not activate the plugin.

4. Configure your HTTPS reverse proxy to forward the exact `/alice/<YOUR_64_HEX_RANDOM_SECRET>` path to the loopback listener on the configured port. In your **private** Alice skill, set the corresponding HTTPS webhook using your own domain and secret. Keep this URL out of screenshots, issues, shell history, proxy access logs, and analytics. No public endpoint or proxy configuration is included here.
5. Restart your gateway using your normal Hermes workflow. Importing or registering the plugin in the CLI does **not** open a port. The listener binds only when the gateway invokes the Telegram connect factory; unload closes it. A plugin reload in a running gateway (`hermes plugins enable`/`disable` of any plugin, the Desktop switch, a tool that re-scans plugins for a missing provider) unloads and registers the plugin again without calling the factory a second time, so the new registration takes the listener over on the same port; disabling the plugin keeps it closed. It binds `127.0.0.1` (default port `8788`), not a public interface. Establish the Telegram session first, then test a harmless voice task and separately confirm its result in Telegram.

## Security and limits

- Intended for **one operator and a private skill**, not a multi-user public service. A secret URL is a bearer credential. The plugin does **not verify a Yandex request signature**. Body skill/account IDs are checks on untrusted claims, not cryptographic identity; someone with the bearer can forge them.
- Injected text is ordinary user input with the permissions of the configured Hermes session. Payload role, model, tools, and destination fields cannot override the fixed route. Leading slash commands and control characters are rejected, but ordinary-language requests can still ask the agent to use tools. Keep normal approvals and gateway authorization in force.
- Default profile and Telegram DM only. Core estop blocks new task injection; greeting/help/exit/control reminders remain local. Alice cannot approve actions or manage Hermes.
- Deduplication is an in-memory cache of the last **32** accepted message keys. Eviction/restart loses protection; this is **not exactly-once delivery**. A scheduled message can subsequently fail authorization/routing, and a cached ACK is not a receipt.
- Text limit: 4,096 characters per field; body limit: 16,384 bytes; JSON POST only. The simple single-threaded HTTP server has no durable queue, global rate limiter, or high-availability guarantee. Keep it behind a secured proxy with appropriate limits.
- The plugin suppresses its HTTP request logging. Your proxy, platform, gateway, session history, and model provider have separate privacy/logging policies. Secret rotation and disabling the plugin require a gateway restart; rotate after disclosure.

## Tests

From the repository root:

```sh
python -B -m unittest discover -s plugins/alice-voice -v
```

Public CI runs on Python 3.11, 3.12, and 3.13: **18 bridge + 4 control tests** with synthetic IDs and ephemeral bearers. Loopback HTTP tests use temporary ports, no Telegram/Yandex connection. The optional SDK wrapper skips unless explicitly configured; a skip is not an integration pass.

For a reviewed Hermes checkout/runtime with dependencies installed:

```sh
ALICE_TEST_ENGINE_ROOT="<YOUR_HERMES_ENGINE_ROOT>" \
ALICE_TEST_ENGINE_PYTHON="<YOUR_HERMES_PYTHON_EXECUTABLE>" \
python -B -m unittest discover -s plugins/alice-voice -p test_sdk_http.py -v
```

The optional test checks the **current** three source hashes before copying SDK/gateway packages into a temporary tree. Unsupported trees/versions raise a clear `SkipTest`; missing runtime dependencies fail rather than fabricate success. The child receives a cleared environment and temporary Hermes home. Seven child checks exercise stock scheduling/dispatch/turn methods with synthetic authorization, model worker, store, transport, and estop fixtures. They do **not** call a real LLM or deliver Telegram messages. No backup files are required or restored.

## Project

- `plugins/alice-voice/`: three runtime files and local tests.
- [Changelog](CHANGELOG.md), [license](LICENSE), [third-party notices](THIRD_PARTY_NOTICES.md).
- No SDK diffs, patch installers, deployment helpers, or production configuration are distributed.

MIT © 2026 Ilya Balobanov. Independent integration; not an official Yandex or Nous Research product.
