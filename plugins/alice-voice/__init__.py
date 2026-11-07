"""Voice input only, stock SDK. No listener on import/CLI; existing Telegram only."""
from .bridge import AliceBridge


def register(ctx):
    bridge = AliceBridge(ctx)
    if not bridge.enabled:
        return
    ctx.register_telegram_handler(bridge.on_telegram_connect)
    # None: the engine abandoned this load past its deadline and ignores registrations from now on,
    # so nothing would ever close a listener started here.
    if ctx.on_unload(bridge.close) is not None:
        bridge.adopt()  # a plugin reload: the engine will not call the factory again
