from __future__ import annotations

from gamemanager.services.storefronts.base import StorePlugin
from gamemanager.services.storefronts.stub_connector import StubLauncherConnector


class UbisoftConnector(StubLauncherConnector):
    store_name = "Ubisoft"


PLUGIN = StorePlugin(
    store_name="Ubisoft",
    connector_cls=UbisoftConnector,
    auth_kind="launcher_import",
    supports_full_library_sync=False,
    description="Ubisoft connector using launcher/cache data.",
)
