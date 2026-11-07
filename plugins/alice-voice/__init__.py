"""Voice input only, stock SDK. No listener on import/CLI; existing Telegram only."""
from .bridge import AliceBridge


def register(ctx):
    bridge = AliceBridge(ctx)
    if not bridge.enabled:
        return
    ctx.register_telegram_handler(bridge.on_telegram_connect)
    ctx.on_unload(bridge.close)
    bridge.take_over_after_reload()  # a plugin reload: the engine will not call the factory again
