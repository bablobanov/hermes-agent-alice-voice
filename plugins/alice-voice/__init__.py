"""Voice input only, stock SDK. No listener on import/CLI; existing Telegram only."""
from .bridge import AliceBridge


def register(ctx):
    bridge = AliceBridge(ctx)
    if not bridge.enabled:
        return
    # The unload callback first. None: the engine abandoned this load past its deadline and ignores every
    # registration from now on, so nothing would close a listener started for it. Accepted, a factory
    # registered after it always has its cleanup; a deadline firing later disposes the callback, which
    # closes the bridge.
    if ctx.on_unload(bridge.close) is None:
        return
    ctx.register_telegram_handler(bridge.on_telegram_connect)
    bridge.adopt()  # a plugin reload: the engine will not call the factory again
