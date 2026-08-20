"""The law firm's exact read-only third-party connector catalog."""
from __future__ import annotations

from . import Tool
from ._connector_specs import _SPECS
from ._rest_connector import _build_auth_headers, make_rest_tool

READ_CONNECTOR_NAMES: list[str] = [spec["name"] for spec in _SPECS]
READ_CONNECTOR_RISKS: dict[str, str] = dict.fromkeys(READ_CONNECTOR_NAMES, "low")
# Compatibility export for callers that distinguish write-capable enterprise
# adapters. The firm ships none.
ENTERPRISE_CONNECTOR_NAMES: list[str] = []


def data_connectors_for_suite(suite: str | None) -> frozenset[str]:
    """Return no ambient suite-wide grant.

    Each legal profile must declare an exact connector; selecting the legal
    suite alone must not grant every firm's system to every specialist.
    """
    del suite
    return frozenset()


def auth_headers_for(
    connector: str,
    token: str,
    *,
    include_extra_headers_env: bool = True,
) -> dict[str, str]:
    """Build the retained connector's authorization headers.

    Unknown names fall back to Bearer for the existing connection-probe API,
    while no unknown connector can be instantiated by this module.
    """
    del include_extra_headers_env
    spec = next((item for item in _SPECS if item["name"] == connector), {})
    return _build_auth_headers(
        token,
        basic=bool(spec.get("basic")),
        token_header=str(spec.get("token_header", "Authorization")),
        scheme=str(spec.get("scheme", "Bearer")),
        extra_headers_env=None,
    )


def enterprise_connectors() -> list[Tool]:
    """Instantiate exactly five GET-only legal-system tools."""
    return [make_rest_tool(read_only=True, **spec) for spec in _SPECS]


def _label(name: str) -> str:
    return {
        "carta_read": "Carta (read only)",
        "clio_read": "Clio (read only)",
        "contractbook_read": "Contractbook (read only)",
        "docusign_read": "DocuSign (read only)",
        "ironclad_read": "Ironclad (read only)",
    }[name]


def connector_catalog() -> list[dict]:
    """Installer metadata for the five retained connector credentials."""
    return [
        {
            "name": spec["name"],
            "label": _label(spec["name"]),
            "env": [
                (spec["base_url_env"], False),
                (spec["token_env"], True),
            ],
        }
        for spec in _SPECS
    ]


__all__ = [
    "ENTERPRISE_CONNECTOR_NAMES",
    "READ_CONNECTOR_NAMES",
    "READ_CONNECTOR_RISKS",
    "auth_headers_for",
    "connector_catalog",
    "data_connectors_for_suite",
    "enterprise_connectors",
]
