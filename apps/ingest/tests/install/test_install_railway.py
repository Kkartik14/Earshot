"""Railway: ``deploy/railway/railway.toml`` against Railway's published schema.

Railway's schema (https://railway.com/railway.schema.json) allows keys it does not
declare, so a misspelt setting would validate and then be ignored: every key must
also be one the schema declares. The settings the one-server rule depends on are
present: one replica, and a required volume at ``/data`` (Railway never keeps two
deployments of a service with a volume active at once, so a redeploy cannot
overlap the old server's database lock). This is a schema check only; a real
deploy and redeploy is a maintainer's check.
"""

from __future__ import annotations

import json
import tomllib
import urllib.request
from typing import Any

import pytest
from install_support import OPT_IN_ENVIRONMENT, RAILWAY_CONFIG, ROOT, require_install_tests

pytestmark = [pytest.mark.integration]

SCHEMA_URL = "https://railway.com/railway.schema.json"


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    require_install_tests()
    # Railway's edge refuses Python's default User-Agent.
    request = urllib.request.Request(SCHEMA_URL, headers={"User-Agent": "earshot-install-tests"})
    with urllib.request.urlopen(request, timeout=30) as reply:
        return json.load(reply)


def _undeclared_keys(value: Any, schema: dict[str, Any], path: tuple[str, ...] = ()) -> list[str]:
    """Keys of ``value`` (recursively) that ``schema`` does not declare."""

    if not isinstance(value, dict):
        return []
    candidates = [schema, *schema.get("anyOf", []), *schema.get("oneOf", [])]
    properties = next((item["properties"] for item in candidates if "properties" in item), None)
    if properties is None:
        return []
    found = []
    for key, item in value.items():
        if key in properties:
            found += _undeclared_keys(item, properties[key], (*path, key))
        else:
            found.append(".".join((*path, key)))
    return found


def test_the_railway_config_validates_against_railways_schema(schema: dict[str, Any]) -> None:
    try:
        import jsonschema
    except ImportError:
        pytest.fail(f"{OPT_IN_ENVIRONMENT}=1 needs the jsonschema package")
    config = tomllib.loads(RAILWAY_CONFIG.read_text(encoding="utf-8"))

    jsonschema.Draft202012Validator(schema).validate(config)
    assert _undeclared_keys(config, schema) == []
    # The key check itself catches a misspelling.
    assert _undeclared_keys({"deploy": {"healthCheckPath": "/"}}, schema) == [
        "deploy.healthCheckPath"
    ]

    assert (ROOT / config["build"]["dockerfilePath"]).is_file()
    deploy = config["deploy"]
    assert deploy["numReplicas"] == 1
    assert deploy["requiredMountPath"] == "/data"
    assert deploy["healthcheckPath"] == "/readyz"
