"""Run durable message retries and scheduled conversation-retention cleanup."""

from __future__ import annotations

import argparse
import logging
import os
import time

from hermes.runtime import Runtime
from hermes.worker import (
    expire_inventory_reservations,
    process_billing_reconciliation,
    process_outbound_queue,
    process_scheduled_followups,
    purge_expired_data,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=10, help="Worker polling interval in seconds")
    parser.add_argument(
        "--retention-days",
        type=int,
        default=int(os.getenv("HERMES_RETENTION_DAYS", "365")),
        help="Delete conversations inactive for this many days",
    )
    parser.add_argument("--once", action="store_true", help="Run one queue and retention pass, then exit")
    args = parser.parse_args()
    if args.interval <= 0 or args.retention_days < 1:
        parser.error("interval must be positive and retention-days must be at least one")

    logging.basicConfig(level=os.getenv("HERMES_LOG_LEVEL", "INFO"))
    runtime = Runtime()
    next_retention = 0.0
    while True:
        processed = (
            process_outbound_queue(runtime)
            + process_scheduled_followups(runtime)
            + process_billing_reconciliation(runtime)
        )
        expired_reservations = expire_inventory_reservations(runtime)
        if expired_reservations:
            logging.info("Released %d expired stock reservations", expired_reservations)
        now = time.time()
        if now >= next_retention:
            removed = purge_expired_data(runtime, args.retention_days)
            logging.info("Retention pass removed %d conversations", removed)
            next_retention = now + 24 * 60 * 60
        if args.once:
            logging.info("Worker pass processed %d outbound messages", processed)
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
