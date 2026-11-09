# Changelog

## Unreleased

- The voice input survives a plugin reload. Any forced plugin re-discovery in a running gateway
  (`hermes plugins install`/`enable`/`update` of any plugin, `disable` too from Hermes 0.23, the
  Desktop switch, a TTS or image tool that re-scans plugins for a missing provider) unloads the
  plugin, which closes the listener, and registers it again. Hermes does not call a connect factory
  it has already wired on the same Telegram client, and after a disable no re-wire runs at all, so
  the listener stayed down until a gateway restart (Hermes 0.22.2 and 0.23.0 alike). Now the first
  Telegram connect records a process-wide fact, "this is a gateway that connected Telegram", and
  every later registration of the enabled plugin listens at once on the same port; a connect that
  reaches an unloaded bridge (a factory list taken before the unload) starts its successor. A fact,
  not a one-shot token, so no interleaving of unloads, registrations and late connects can lose it.
  A disabled plugin keeps the port closed; a load the engine abandoned past its deadline registers
  no connect factory and never listens; a failed bind is retried by the next reload or connect. Nine
  regression tests; 20 bridge + 4 control tests. Updating from 0.3.0 inside a running gateway: 0.3.0
  does not record the Telegram connect, so the listener comes back after one gateway restart (or a
  Telegram reconnect); later reloads keep it.

## 0.3.0

- Minimal public release of private Alice voice input using stock Hermes gateway injection.
- Fixed existing Telegram DM destination in the default profile; Telegram-only results.
- Listener starts on the Telegram connect factory, never import or CLI registration;
  unload closes it. Core estop blocks new task injection.
- Asynchronous scheduling ACK, 32-entry in-memory retry cache, local control reminders.
- No completion callbacks, result reader, SDK patches, or Alice TTS.
- 15 local unit tests and an optional hash-verified current-stock SDK/HTTP probe
  with seven synthetic child checks. Public CI covers Python 3.11–3.13 unit tests.

The operator-reported successful real chain is not a blanket live-case verification.
