"""运行清单与报告 JSON：任何结论都能被第三方按清单复算。"""
from __future__ import annotations

import json


def _payload(run: dict) -> dict:
    return {
        "run_id": run["run_id"],
        "manifest": run["manifest"],
        "accounts": run["summary"].get("accounts", {}),
        "checks": run["summary"].get("checks", []),
        "watermark": run["summary"].get("watermark", ""),
        "rules_version": run["rules_version"],
        "reason_counts": run["summary"].get("reason_counts", []),
    }


def manifest_json(run: dict) -> str:
    return json.dumps(_payload(run), ensure_ascii=False, indent=2, sort_keys=True)


def manifest_json_with_adjudication(run: dict, accounts: dict, adjudication: dict,
                                    groups: list[dict]) -> str:
    """清单叠加裁定层：原报账目原样保留，另给有效态账目与决定链。

    审计要能一眼分清「原报事实」与「人工决定」，所以不做覆盖，只并列。
    """
    payload = _payload(run)
    payload["adjudication"] = {
        "accounts_effective": accounts,
        "revenue": adjudication,
        "groups": groups,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
