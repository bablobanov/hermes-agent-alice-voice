# Changelog

## Unreleased

- The voice input survives a plugin reload. `hermes plugins enable`/`disable` of any plugin and the
  Desktop switch send a running gateway `reload-plugins`: the plugin is unloaded (the listener closes)
  and registered again, but Hermes does not call the same connect factory twice on one Telegram client,
  so the listener stayed down until a gateway restart (Hermes 0.22.2 and 0.23.0 alike). The unload now
  leaves a process-wide marker and the next registration takes the listener over on the same port. A
  disabled plugin keeps it closed. Two regression tests; 13 bridge + 4 control tests.

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
