"""The HA assets, checked against the application they claim to run.

A Helm chart and a compose file are configuration, and configuration rots
silently: a probe path is renamed, a replica count is dropped to one, an image
tag becomes `latest`, and nothing fails until an incident. These tests are what
notice.

They do not prove the stack is highly available — that needs more than one
host, and `deploy/ha/README.md` says so. They prove the assets still describe
*this* application: the probes point at endpoints that exist, the read-only
allow-list names real routes, and everything that must be more than one is.
"""

from pathlib import Path

import pytest
import yaml

ASSETS = Path(__file__).resolve().parents[3] / "deploy" / "ha"
CHART = ASSETS / "helm" / "sutr"


def _yaml(path: Path):
    return yaml.safe_load(path.read_text())


# ── The compose stack ────────────────────────────────────────────────────────


@pytest.fixture(scope="module", name="compose")
def compose_fixture() -> dict:
    return _yaml(ASSETS / "docker-compose.ha.yml")


def test_the_stack_runs_more_than_one_of_the_application(compose):
    services = compose["services"]
    api_services = [name for name in services if name.startswith("api-")]
    assert len(api_services) >= 2, "an HA stack with one replica is a stack"


def test_the_application_talks_to_the_pooler_rather_than_the_database(compose):
    """The point of a pooler is that replica count and connection count stop
    being the same number."""
    url = compose["x-api-environment"]["DATABASE_URL"]
    assert "pgbouncer" in url
    assert "postgres-primary" not in url


def test_migrations_run_once_before_any_replica_serves(compose):
    """Two replicas racing `alembic upgrade head` is a way to corrupt a schema."""
    migrate = compose["services"]["migrate"]
    assert migrate["command"] == ["uv", "run", "alembic", "upgrade", "head"]
    for name in ("api-1", "api-2", "relay"):
        depends = compose["services"][name].get("depends_on", {})
        assert depends.get("migrate", {}).get("condition") == "service_completed_successfully"


def test_the_replica_database_is_a_streaming_replica_not_a_second_database(compose):
    command = compose["services"]["postgres-replica"]["command"]
    assert "pg_basebackup" in command
    # -R writes the connection settings and standby.signal, which is what makes
    # it follow rather than diverge.
    assert "-R" in command


def test_every_image_is_pinned(compose):
    for name, service in compose["services"].items():
        image = service.get("image")
        if image is None or image == "sutr:ha":
            continue  # built from this repository by the stack itself
        assert ":" in image, f"{name} has no tag"
        assert not image.endswith(":latest"), f"{name} is pinned to latest"


def test_the_load_balancer_health_checks_readiness_not_liveness():
    """A replica that has lost the database should leave the rotation while
    staying alive to be looked at."""
    caddyfile = (ASSETS / "Caddyfile.ha").read_text()
    assert "health_uri /health/ready" in caddyfile
    assert "api-1:4747 api-2:4747" in caddyfile


def test_the_statement_timeout_is_set_on_the_role_not_on_the_connection(compose):
    """Found live: PgBouncer refuses startup parameters it does not know, and
    every replica died at boot with "unsupported startup parameter in options:
    statement_timeout". Behind a pooler the timeout belongs on the role."""
    assert "DB_STATEMENT_TIMEOUT_MS" not in compose["x-api-environment"]
    init = (ASSETS / "postgres" / "init-replication.sh").read_text()
    assert "statement_timeout" in init


def test_replication_has_a_role_of_its_own(compose):
    """The replica streams WAL; it has no business holding the application's
    credentials to do it."""
    init = (ASSETS / "postgres" / "init-replication.sh").read_text()
    assert "CREATE ROLE replicator WITH REPLICATION LOGIN" in init
    hba = (ASSETS / "postgres" / "pg_hba.conf").read_text()
    assert "host    replication     replicator" in hba


# ── The Helm chart ───────────────────────────────────────────────────────────


@pytest.fixture(scope="module", name="values")
def values_fixture() -> dict:
    return _yaml(CHART / "values.yaml")


def test_the_chart_declares_the_application_version_it_was_written_against():
    chart = _yaml(CHART / "Chart.yaml")
    assert chart["apiVersion"] == "v2"
    assert chart["version"]
    assert chart["appVersion"]


def test_the_default_is_more_than_one_replica(values):
    assert values["api"]["replicas"] > 1
    assert values["podDisruptionBudget"]["minAvailable"] >= 1


def test_the_chart_ships_no_credentials(values):
    """A chart with a default password is a chart that deploys one."""
    assert values["existingSecret"] == ""
    rendered = " ".join(
        path.read_text() for path in (CHART / "templates").iterdir() if path.is_file()
    )
    assert "kind: Secret" not in rendered


def test_the_chart_refuses_to_render_without_an_image(values):
    assert values["image"]["repository"] == ""
    assert values["image"]["tag"] == ""
    helpers = (CHART / "templates" / "_helpers.tpl").read_text()
    assert "image.repository is required" in helpers
    assert "image.tag is required" in helpers
    # A rolling tag means two replicas of one release can run different code.
    assert 'eq .Values.image.tag "latest"' in helpers


def test_the_probes_point_at_endpoints_the_application_serves():
    from sutr.main import app
    from tests.conftest import served_paths

    paths = served_paths(app)
    deployment = (CHART / "templates" / "deployment-api.yaml").read_text()

    for probe, path in (
        ("livenessProbe", "/health/live"),
        ("readinessProbe", "/health/ready"),
        ("startupProbe", "/health/live"),
    ):
        assert probe in deployment
        assert path in paths, f"{probe} points at {path}, which is not a route"

    # Liveness must not be the readiness path: restarting every replica during
    # a database outage is how a recoverable incident gets longer.
    liveness_block = deployment.split("livenessProbe:")[1].split("readinessProbe:")[0]
    assert "/health/live" in liveness_block
    assert "/health/ready" not in liveness_block


def test_a_rollout_never_drops_below_the_declared_replicas():
    deployment = (CHART / "templates" / "deployment-api.yaml").read_text()
    assert "maxUnavailable: 0" in deployment


def test_migrations_are_a_hook_that_runs_before_the_new_replicas():
    job = (CHART / "templates" / "job-migrate.yaml").read_text()
    assert '"helm.sh/hook": pre-install,pre-upgrade' in job
    assert "parallelism: 1" in job
    assert "alembic" in job


def test_the_pods_are_given_no_kubernetes_credentials():
    """The application talks to no Kubernetes API. A mounted token would be a
    credential an attacker inside the Pod would find."""
    for name in ("deployment-api.yaml", "deployment-relay.yaml", "job-migrate.yaml"):
        assert "automountServiceAccountToken: false" in (CHART / "templates" / name).read_text()


def test_the_relay_deployment_relies_on_the_lease_rather_than_on_a_flag():
    """Two relay Pods are safe because they contend for one lease. That is the
    claim; this is the assertion that the chart is written that way."""
    relay = (CHART / "templates" / "deployment-relay.yaml").read_text()
    assert "replicas: {{ .Values.relay.replicas }}" in relay
    values = _yaml(CHART / "values.yaml")
    assert values["relay"]["replicas"] > 1


def test_the_chart_config_keys_are_read_by_something(values):
    """A ConfigMap key nothing reads is a setting somebody will believe they
    configured. Every key is either a settings field or a variable the
    container's entrypoint acts on."""
    from sutr.config import Settings

    known = {name.upper() for name in Settings.model_fields}
    entrypoint = (Path(__file__).resolve().parents[2] / "start.sh").read_text()

    for key in values["config"]:
        assert key in known or key in entrypoint, (
            f"{key} is in the chart's config, in no setting, and in no entrypoint"
        )


def test_the_notes_do_not_claim_what_was_never_run():
    notes = (CHART / "templates" / "NOTES.txt").read_text()
    assert "has not been run against a live cluster" in notes


# ── Backup and restore ───────────────────────────────────────────────────────


def test_the_backup_records_the_schema_version_it_was_taken_at():
    """A dump whose schema version nobody wrote down is a dump somebody will
    restore into the wrong code."""
    backup = (ASSETS / "backup" / "backup.sh").read_text()
    assert "alembic_version" in backup
    assert "sha256" in backup


def test_the_restore_verifies_rather_than_merely_finishing():
    restore = (ASSETS / "backup" / "restore.sh").read_text()
    # Checksum, schema version, and that anything came back at all.
    assert "checksum mismatch" in restore
    assert "schema version mismatch" in restore
    assert "the restore produced no tables" in restore
    # And refuses to overwrite a database that has data, unless told to.
    assert "FORCE:-0" in restore


def test_the_backup_says_it_does_not_encrypt():
    """LLD §5.4 asks for encrypted backups. This script does not encrypt, and
    says where the encryption belongs instead of implying it happened."""
    backup = (ASSETS / "backup" / "backup.sh").read_text()
    assert '"encrypted": false' in backup
    assert "does not encrypt" in backup


# ── What the README may not claim ────────────────────────────────────────────


def test_the_readme_does_not_claim_availability_it_has_not_measured():
    readme = (ASSETS / "README.md").read_text()
    assert "NOT TESTED" in readme or "not measured" in readme
    assert "no automatic failover" in readme.lower()


def test_the_dashboard_and_alerts_still_belong_to_the_other_stack():
    """Phase 11's observability assets and these are separate deployments on
    purpose; nothing here should have grown a copy."""
    assert not (ASSETS / "alerts.yml").exists()
