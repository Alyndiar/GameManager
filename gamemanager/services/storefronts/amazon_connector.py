from __future__ import annotations

from gamemanager.services.storefronts.base import StorePlugin
from gamemanager.services.storefronts.stub_connector import StubLauncherConnector


class AmazonConnector(StubLauncherConnector):
    store_name = "Amazon Games"


PLUGIN = StorePlugin(
    store_name="Amazon Games",
    connector_cls=AmazonConnector,
    auth_kind="browser_oauth",
    supports_full_library_sync=True,
    description="Amazon Games connector scaffold for entitlement sync.",
)
