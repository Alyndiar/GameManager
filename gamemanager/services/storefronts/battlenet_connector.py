from __future__ import annotations

from gamemanager.services.storefronts.base import StorePlugin
from gamemanager.services.storefronts.stub_connector import StubLauncherConnector


class BattleNetConnector(StubLauncherConnector):
    store_name = "Battle.net"


PLUGIN = StorePlugin(
    store_name="Battle.net",
    connector_cls=BattleNetConnector,
    auth_kind="browser_session",
    supports_full_library_sync=True,
    description="Battle.net connector scaffold for account-linked library sync.",
)
