"""Supplemental metadata contracts; canonical analysis-result v1 is unchanged."""

import copy


def record(properties):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


TEXT = {"type": "string", "minLength": 1}
NULLABLE = {"type": ["string", "null"]}
STRINGS = {"type": "array", "items": TEXT, "uniqueItems": True}
ENVIRONMENT_SCHEMA = record(
    {
        "key": {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]*$"},
        "component": TEXT,
        "serviceName": NULLABLE,
        "phase": {"enum": ["build", "runtime", "unknown"]},
        "required": {"type": ["boolean", "null"]},
        "origin": {"enum": ["source", "compose", "dockerfile", "example"]},
        "evidenceIds": STRINGS,
        "condition": NULLABLE,
    }
)
BUILD_TARGET_SCHEMA = record(
    {
        "id": TEXT,
        "component": TEXT,
        "serviceName": NULLABLE,
        "composePath": NULLABLE,
        "contextPath": NULLABLE,
        "contextBasis": {"enum": ["compose", "package_directory", "root_default_policy", "unresolved"]},
        "dockerfilePath": NULLABLE,
        "target": NULLABLE,
        "status": {"enum": ["detected", "needs_input"]},
        "condition": NULLABLE,
        "steps": {
            "type": "array",
            "items": record(
                {
                    "phase": {"enum": ["install", "build", "other"]},
                    "command": TEXT,
                    "workingDirectory": NULLABLE,
                    "stage": NULLABLE,
                    "evidenceIds": STRINGS,
                }
            ),
        },
        "runtimeWorkingDirectory": NULLABLE,
        "packageManager": NULLABLE,
        "packageManagerVersion": NULLABLE,
        "lockfiles": STRINGS,
        "installCommand": NULLABLE,
        "installCommandBasis": {"enum": ["declared", "policy", "unknown"]},
        "buildCommand": NULLABLE,
        "buildCommandBasis": {"enum": ["declared", "policy", "unknown"]},
        "buildWorkingDirectory": NULLABLE,
        "outputPaths": STRINGS,
        "evidenceIds": STRINGS,
        "unresolved": STRINGS,
    }
)
CONNECTION_SCHEMA = record(
    {
        "fromComponent": TEXT,
        "fromService": TEXT,
        "toService": TEXT,
        "protocol": TEXT,
        "port": {"type": ["integer", "null"], "minimum": 1, "maximum": 65535},
        "environmentKey": TEXT,
        "condition": NULLABLE,
        "evidenceIds": STRINGS,
    }
)


def execution_metadata(bundle: dict) -> dict:
    """Expose source-grounded names/paths; never expand or reconstruct secrets."""
    keys = {
        "deployment.build_target": "buildTargets",
        "environment.consumer": "environmentVariables",
        "deployment.connection": "serviceConnections",
    }
    result = {key: [] for key in keys.values()}
    for fact in bundle["facts"]:
        if fact["key"] in keys and isinstance(fact["value"], dict):
            item = copy.deepcopy(fact["value"])
            item["evidenceIds"] = list(fact["evidenceIds"])
            result[keys[fact["key"]]].append(item)
    return result
