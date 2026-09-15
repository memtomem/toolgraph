"""Runtime health diagnosis (doctor) for configuration, connectivity, and schema."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from toolgraph import config
from toolgraph.graph import driver, queries, schema
from toolgraph.redaction import redact_text


@dataclass
class DoctorCheck:
    name: str
    status: str  # "PASS", "WARN", "FAIL"
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class DoctorReport:
    healthy: bool
    checks: list[DoctorCheck] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary_text(self) -> str:
        lines = ["Toolgraph Doctor Diagnosis:", "============================"]
        for c in self.checks:
            mark = f"[{c.status}]"
            lines.append(f"  {mark:6s} {c.name}: {c.message}")
            if c.details:
                for k, v in c.details.items():
                    lines.append(f"         - {k}: {v}")
        status_str = "HEALTHY" if self.healthy else "UNHEALTHY"
        lines.append("----------------------------")
        lines.append(f"Overall status: {status_str}")
        return "\n".join(lines)


def run_doctor() -> DoctorReport:
    checks: list[DoctorCheck] = []
    overall_healthy = True

    # 1. Configuration check
    try:
        cfg_info = config.check_config_health()
    except Exception as exc:
        error = redact_text(exc)
        return DoctorReport(healthy=False, checks=[DoctorCheck(
            name="Configuration", status="FAIL",
            message=f"Failed to inspect configuration: {error}", details={"error": error},
        )])
    cfg_info = {key: redact_text(value) if isinstance(value, str) else value for key, value in cfg_info.items()}
    if not cfg_info["exists"]:
        # Missing config file is a non-fatal warning if running on environment defaults
        checks.append(
            DoctorCheck(
                name="Configuration",
                status="WARN",
                message=f"No config file at {cfg_info['path']} (using environment/defaults)",
                details=cfg_info,
            )
        )
    elif not cfg_info["valid_json"]:
        checks.append(
            DoctorCheck(
                name="Configuration",
                status="FAIL",
                message=f"Invalid config JSON at {cfg_info['path']}: {cfg_info['detail']}",
                details=cfg_info,
            )
        )
        return DoctorReport(healthy=False, checks=checks)
    elif cfg_info["permissions_ok"] is False:
        checks.append(
            DoctorCheck(
                name="Configuration",
                status="WARN",
                message=f"Config file permissions are {cfg_info['permissions']}; recommended 0600",
                details=cfg_info,
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="Configuration",
                status="PASS",
                message=f"Valid config at {cfg_info['path']} (mode {cfg_info['permissions'] or 'n/a'})",
                details=cfg_info,
            )
        )

    # Safely obtain backend name from settings
    backend_name = "unknown"
    try:
        backend_name = redact_text(config.settings.backend)
    except Exception as exc:
        redacted_err = redact_text(exc)
        checks.append(
            DoctorCheck(
                name="Configuration",
                status="FAIL",
                message=f"Failed to load configuration settings: {redacted_err}",
                details={"error": redacted_err},
            )
        )
        return DoctorReport(healthy=False, checks=checks)

    # 2. Connectivity check
    try:
        driver.verify_connectivity()
        checks.append(
            DoctorCheck(
                name="Connectivity",
                status="PASS",
                message=f"Backend '{backend_name}' connected and responsive",
                details={"backend": backend_name},
            )
        )
    except Exception as exc:
        redacted_err = redact_text(exc)
        checks.append(
            DoctorCheck(
                name="Connectivity",
                status="FAIL",
                message=f"Failed to connect to backend '{backend_name}': {redacted_err}",
                details={"backend": backend_name, "error": redacted_err},
            )
        )
        overall_healthy = False
        return DoctorReport(healthy=False, checks=checks)

    # 3. Schema integrity check
    try:
        schema_info = schema.inspect_backend_schema()
        if schema_info["healthy"]:
            msg = f"Schema version {schema_info['schema_version']} intact"
            if schema_info["backend"] == "ladybug":
                msg += f" ({len(schema_info['live_tables'])} tables present)"
            else:
                msg += f" ({len(schema_info['live_constraints'])} constraints present)"
            checks.append(
                DoctorCheck(
                    name="Schema Integrity",
                    status="PASS",
                    message=msg,
                    details=schema_info,
                )
            )
        else:
            missing = schema_info["missing_tables"] or schema_info["missing_constraints"]
            msg = f"Schema issue detected: version_ok={schema_info['version_ok']}, missing={missing}"
            checks.append(
                DoctorCheck(
                    name="Schema Integrity",
                    status="FAIL",
                    message=msg,
                    details=schema_info,
                )
            )
            overall_healthy = False
    except Exception as exc:
        redacted_err = redact_text(exc)
        checks.append(
            DoctorCheck(
                name="Schema Integrity",
                status="FAIL",
                message=f"Failed to inspect schema: {redacted_err}",
                details={"error": redacted_err},
            )
        )
        overall_healthy = False

    # 4. Graph State / Metadata
    try:
        state = queries.graph_state()
        if state.instance_id:
            checks.append(
                DoctorCheck(
                    name="Graph State",
                    status="PASS",
                    message=f"Instance {state.instance_id}, generation {state.generation}",
                    details={
                        "instance_id": state.instance_id,
                        "generation": state.generation,
                        "governance_digest": state.governance_digest or "none (un-ingested)",
                    },
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    name="Graph State",
                    status="WARN",
                    message="No instance_id initialized — run 'toolgraph init-schema' to initialize",
                    details={"generation": state.generation},
                )
            )
    except Exception as exc:
        redacted_err = redact_text(exc)
        checks.append(
            DoctorCheck(
                name="Graph State",
                status="FAIL",
                message=f"Failed to read graph state: {redacted_err}",
                details={"error": redacted_err},
            )
        )
        overall_healthy = False

    return DoctorReport(healthy=overall_healthy, checks=checks)
