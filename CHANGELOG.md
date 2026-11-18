# Changelog

## 0.3.3

- `owner_user_id` is required. Up to 0.3.2 an empty value, the manifest default, let any signed-in Yandex
  account through: the webhook URL and the skill ID were enough to send tasks. Now the bridge stays off
  without it, as with an invalid secret, and the gateway log says why (`alice-voice: bridge is off: set
  owner_user_id …`); a blank value, a placeholder or spaces around the value count as missing, and the
  same check now covers `skill_id`; the manifest marks the key `required`. **Updating: set `owner_user_id` before or with
  the update, or the voice input turns off.**
- Every voice task reaches the session as `[Голосовая задача из Алисы] <spoken text>`, so an agent rule can
  ask for a Telegram confirmation of irreversible actions from this channel. C1 controls (NEL) and Unicode
  line and paragraph separators are refused along with the C0 controls refused before: the spoken text
  stays one line and cannot fake a separate message.
- At most 5 tasks per sliding minute are scheduled, each slot held a minute from the moment its task was
  scheduled; above that Alice answers "Слишком много задач подряд.
  Попробуйте через минуту" with HTTP 200, so Alice voices it. In memory, per loaded plugin; a retry of an
  accepted task does not count.
- README (EN, RU) and SECURITY.md: the threat model as it is. Three checks, Yandex does not sign skill
  requests, what a person with the URL and the IDs can and cannot do (nothing comes back through Alice, but a
  forged task can ask the agent for anything its tools allow without approval), the proxy that the
  single-threaded listener needs, secret rotation after a leak.
- 26 bridge + 4 control tests.

## 0.3.2

- The plugin folder carries its own README, `plugins/alice-voice/README.md`: the repository README with its
  links pointing up to the root. It is for the Hermes plugin marketplace, which shows "no README" for catalog
  entries whose README is only at the repository root (NousResearch/hermes-agent#149607). No code changes.

## 0.3.1

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
