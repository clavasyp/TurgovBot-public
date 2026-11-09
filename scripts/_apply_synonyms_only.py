"""Compatibility entry point for deterministic synonym-only plan apply.

Raw ``[{id, synonyms}]`` files are intentionally no longer accepted: they do
not bind source/synonym fingerprints and cannot prove full-server,
batch-invariant collision resolution.  Build a reviewed plan with
``backfill_law_synonyms.py --aggregate-manifest`` and apply it here or through
that CLI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from database.manager import db_manager
from scripts.backfill_law_synonyms import (
    CHECKPOINT_SCHEMA,
    PLAN_SCHEMA,
    _Checkpoint,
    _load_json,
    _verify_hash,
    apply_plan,
    revalidate_checkpoint_result,
)
from services.law_synonym_service import SynonymWriteError


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply one immutable reviewed synonym-only plan"
    )
    parser.add_argument("server_id")
    parser.add_argument("plan_file", type=Path)
    parser.add_argument("--checkpoint", type=Path, required=True)
    args = parser.parse_args()

    try:
        plan = _load_json(args.plan_file)
        _verify_hash(plan, "plan_hash", PLAN_SCHEMA)
        if plan.get("server_id") != args.server_id:
            raise SynonymWriteError("plan belongs to another server")
        checkpoint = _Checkpoint(
            args.checkpoint,
            str(plan["run_id"]),
            {
                "schema": CHECKPOINT_SCHEMA,
                "server_id": args.server_id,
                "plan_hash": plan["plan_hash"],
                "mode": plan["mode"],
            },
        )
        prior = checkpoint.result()

        await db_manager.connect()
        try:
            if prior is not None:
                result = await revalidate_checkpoint_result(plan, prior)
            else:
                result = await apply_plan(plan)
        finally:
            await db_manager.close()
        if prior is None:
            checkpoint.append(result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    except (SynonymWriteError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"REFUSED: {exc}\n")


if __name__ == "__main__":
    asyncio.run(main())
