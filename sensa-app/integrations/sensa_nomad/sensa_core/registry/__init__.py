"""Device registry — manages discoveries and confirmed devices."""

from sensa_core.registry.devices import (
    RegistryFullError,
    clear_devices,
    confirm_device,
    count_active_devices,
    delete_device,
    get_device,
    list_devices,
    register_device,
    update_device,
)
from sensa_core.registry.discoveries import (
    clear_discoveries,
    get_discovery,
    list_discoveries,
    make_discovery_id,
    prune_stale_discoveries,
    upsert_discovery,
)
from sensa_core.registry.store import (
    load_registry,
    registry_transaction,
    save_registry,
)
from sensa_core.registry.summary import get_summary

__all__ = [
    "RegistryFullError",
    "clear_devices",
    "clear_discoveries",
    "confirm_device",
    "count_active_devices",
    "delete_device",
    "get_device",
    "get_discovery",
    "get_summary",
    "list_devices",
    "list_discoveries",
    "load_registry",
    "make_discovery_id",
    "prune_stale_discoveries",
    "register_device",
    "registry_transaction",
    "save_registry",
    "update_device",
    "upsert_discovery",
]
