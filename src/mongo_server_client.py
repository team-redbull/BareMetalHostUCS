"""
MongoDB client for the server_scanner.servers collection.

Provides MAC address, BMC address, and installation status for servers
pre-scanned by the Scan_Servers CronJob. Used as the primary data source;
the vendor API (UnifiedServerClient) is the fallback when a server is not
found here.
"""

import logging
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient

logger = logging.getLogger(__name__)


class MongoServerClient:
    """
    Async Motor client for the server_scanner.servers collection.

    Expected document fields:
        _id              — server profile name (lowercase)
        bmc_address      — BMC/iDRAC/iLO IP address
        mac_address      — NIC MAC address
        installed        — bool: True if server is already deployed in a cluster
        deployed_cluster — cluster/MCE name where server is installed
        vendor           — "HP" / "Dell" / "Cisco"
        maintenance      — maintenance sub-document (if server is in maintenance)
    """

    def __init__(self, uri: str, db_name: str = "server_scanner"):
        self._client = AsyncIOMotorClient(uri)
        self._col = self._client[db_name]["servers"]
        logger.info(f"MongoServerClient initialised — db={db_name}, collection=servers")

    def is_configured(self) -> bool:
        return self._client is not None

    async def get_server(self, name: str) -> Optional[dict]:
        """
        Look up a server by name (case-insensitive).

        Returns the raw MongoDB document dict, or None if not found.
        Callers should check: bmc_address, mac_address, installed, deployed_cluster.
        """
        key = name.lower().strip()
        doc = await self._col.find_one({"_id": key})
        if doc:
            logger.debug(f"MongoDB hit for '{key}': bmc={doc.get('bmc_address')}, "
                         f"mac={doc.get('mac_address')}, installed={doc.get('installed')}")
        else:
            logger.debug(f"MongoDB miss for '{key}'")
        return doc

    async def close(self):
        self._client.close()
        logger.info("MongoServerClient closed")
