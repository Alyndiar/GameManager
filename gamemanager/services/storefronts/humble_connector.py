from __future__ import annotations

from gamemanager.services.storefronts.base import StorePlugin
from gamemanager.services.storefronts.stub_connector import StubLauncherConnector


class HumbleConnector(StubLauncherConnector):
    store_name = "Humble"


PLUGIN = StorePlugin(
    store_name="Humble",
    connector_cls=HumbleConnector,
    auth_kind="browser_session",
    supports_full_library_sync=True,
    description="Humble connector scaffold for account library and key imports.",
)
