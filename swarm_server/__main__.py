"""Entry point: python -m swarm_server"""

import logging

import uvicorn

from swarm_server.config import SERVER_HOST, SERVER_PORT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)

log = logging.getLogger("swarm")

if __name__ == "__main__":
    log.info("=" * 60)
    log.info("  Hermes Swarm Server v0.3.0")
    log.info("  Dashboard:  http://%s:%s/", SERVER_HOST, SERVER_PORT)
    log.info("=" * 60)
    uvicorn.run(
        "swarm_server.server:app",
        host=SERVER_HOST,
        port=SERVER_PORT,
        log_level="info",
        reload=False,
    )
