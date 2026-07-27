"""Run the daily ECPay reconciliation from an imported or stage-downloaded CSV."""
from __future__ import annotations

import argparse
from datetime import date, timedelta
from pathlib import Path

from sqlmodel import Session

from twpay_checkout.config import Settings
from twpay_checkout.db import build_engine, init_db
from twpay_checkout.gateways.ecpay import EcpayGateway
from twpay_checkout.services.operations import (
    build_reconciliation_request,
    post_form,
    reconcile_csv,
)


def _args() -> argparse.Namespace:
    yesterday = date.today() - timedelta(days=1)
    parser = argparse.ArgumentParser(
        description="Import ECPay CSV V3 and persist a reconciliation report."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path, help="Existing UTF-8 ECPay CSV V3")
    source.add_argument(
        "--download-stage",
        action="store_true",
        help="Download from ECPay stage (requires vendor IP allowlist)",
    )
    parser.add_argument("--begin", type=date.fromisoformat, default=yesterday)
    parser.add_argument("--end", type=date.fromisoformat, default=yesterday)
    return parser.parse_args()


def main() -> int:
    args = _args()
    if args.begin > args.end:
        raise SystemExit("--begin must be <= --end")
    settings = Settings()
    engine = build_engine(settings.database_url)
    init_db(engine)
    if args.file:
        csv_text = args.file.read_text(encoding="utf-8-sig")
        source_name = args.file.name
    else:
        gateway = EcpayGateway(settings)
        request = build_reconciliation_request(
            gateway, period_start=args.begin, period_end=args.end
        )
        csv_text = post_form(request.action_url, request.fields)
        source_name = f"ecpay-stage-{args.begin}-{args.end}.csv"

    with Session(engine) as session:
        run = reconcile_csv(
            session,
            csv_text=csv_text,
            source_name=source_name,
            period_start=args.begin,
            period_end=args.end,
        )
        print(
            f"run={run.id} status={run.status.value} "
            f"matched={run.matched_rows} differences={run.difference_rows}"
        )
        if run.error_message:
            print(run.error_message)
        return 0 if run.status.value == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
