# Security

This experimental 0.3.x input bridge is intended for one operator using a private
Alice skill. It is not a public authentication service. Only the current 0.3.x
line is in scope for fixes; there is no guaranteed response time or security SLA.

A request passes three checks: the secret webhook path, the private skill ID and
the owner's Alice account ID (`owner_user_id`, required since 0.3.3; up to 0.3.2 an
empty value let any signed-in account through). The webhook URL is a bearer
credential. Yandex does not sign skill requests: the skill and account IDs are
claims in the request body, and someone with the URL (an access log records it)
and both IDs (logs that record request bodies, the developer console) can forge
them.

Such a person can queue tasks in the owner's fixed Telegram session, at most 5 a
minute (keeping that limit full also refuses the owner's own voice tasks), and gets
nothing back through Alice: replies, approvals and control are in Telegram.
Requests run with the configured Hermes session's existing permissions: whatever the
agent does without asking, including sending data elsewhere with its tools, it may
do for a forged task. Every voice
task starts with `[Голосовая задача из Алисы]`, so an agent rule can require a
Telegram confirmation of irreversible actions from this channel; only a message
without the tag may count as that confirmation.
Keep normal gateway authorization, tool approvals, HTTPS, a proxy that reads whole
requests before passing them on and limits size and time, and log hygiene. See README.md, "Threat model" and "Limits".

Do not publish an exploitable issue or any secrets. Use this repository's GitHub
Security → Report a vulnerability private reporting feature. Share a minimal
synthetic reproducer, affected version, and impact, never your actual bearer URL
or payload identifiers.

If a bearer is disclosed, rotate ALICE_WEBHOOK_SECRET in Hermes .env, update the
private skill webhook, and restart the gateway. Disable the plugin while investigating.
