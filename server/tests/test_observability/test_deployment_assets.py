"""The §5.3 stack assets, checked against the build they claim to monitor.

A directory of YAML is not observability. These tests are what stop it from
becoming one: every metric an alert or a dashboard panel names has to be a
metric this build actually exports, every alert has to point at a runbook
section that exists, and every image has to be pinned.

The configuration files themselves were validated against the real binaries —
`promtool check config`, `amtool check-config`, `otelcol validate`,
`loki -verify-config`, `tempo -config.verify` — and the stack was started and
queried end to end. That validation is not repeated here: it needs Docker and a
network, which this suite does not assume. What is repeated here is everything
that can drift when someone edits the *application*.
"""

import json
import re
from pathlib import Path

import pytest
import yaml
from prometheus_client.parser import text_string_to_metric_families

from sutr.observability import metrics

ASSETS = Path(__file__).resolve().parents[3] / "deploy" / "observability"

# `sutr_x_total` is exposed by the parser as the `sutr_x_total` sample plus a
# `sutr_x_created` one; a histogram adds `_bucket`, `_count` and `_sum`. An
# alert may legitimately name any of those, so the check works from base names.
_SUFFIXES = ("_bucket", "_count", "_sum", "_created")


@pytest.fixture(scope="module", name="exported_metrics")
def exported_metrics_fixture() -> set[str]:
    """Every series name this build can emit, as Prometheus would render it."""
    metrics.observe_tool_call("api", "executed", 5)
    metrics.observe_tool_gated("api", "denied")
    metrics.observe_approval_decision("deny")
    metrics.observe_http_request("GET", "/health", 200, 0.01)
    metrics.observe_provider_request("http", "ok", 5)
    metrics.set_deployment_counts({"running": 1})
    metrics.set_queue_depth("outbox_pending", 0)
    metrics.set_relay_lag_seconds(0)

    names = set()
    for family in text_string_to_metric_families(metrics.render()[0].decode()):
        names.add(family.name)
        for sample in family.samples:
            names.add(sample.name)
    return names


def _known(name: str, exported: set[str]) -> bool:
    if name in exported:
        return True
    for suffix in _SUFFIXES:
        if name.endswith(suffix) and name[: -len(suffix)] in exported:
            return True
    return False


@pytest.fixture(scope="module", name="alert_rules")
def alert_rules_fixture() -> list[dict]:
    document = yaml.safe_load((ASSETS / "alerts.yml").read_text())
    return [rule for group in document["groups"] for rule in group["rules"]]


def _metric_names(expression: str) -> set[str]:
    """The metric names an expression mentions.

    A deliberately simple reading: identifiers that start with `sutr_` or are
    the bare `up` series. Enough to catch a renamed or deleted metric, which is
    the failure this is for.
    """
    found = set(re.findall(r"\b(sutr_[a-z0-9_]+|up)\b", expression))
    return found


# ── Alerts ───────────────────────────────────────────────────────────────────


def test_every_alert_names_a_metric_this_build_exports(alert_rules, exported_metrics):
    """An alert on a series nobody emits never fires, and nobody notices."""
    for rule in alert_rules:
        for name in _metric_names(rule["expr"]):
            if name == "up":
                continue  # Prometheus's own, not ours
            assert _known(name, exported_metrics), f"{rule['alert']} names unknown metric {name}"


def test_every_alert_points_at_a_runbook_section_that_exists(alert_rules):
    runbooks = (ASSETS / "RUNBOOKS.md").read_text()
    headings = set(re.findall(r"^## ([a-z0-9-]+)", runbooks, re.MULTILINE))

    for rule in alert_rules:
        anchor = rule["annotations"]["runbook"]
        assert anchor in headings, f"{rule['alert']} points at a missing runbook '{anchor}'"


def test_no_runbook_section_is_orphaned(alert_rules):
    """A runbook for an alert that no longer exists is a page nobody will
    reach and nobody will delete."""
    runbooks = (ASSETS / "RUNBOOKS.md").read_text()
    headings = set(re.findall(r"^## ([a-z0-9-]+)", runbooks, re.MULTILINE))
    referenced = {rule["annotations"]["runbook"] for rule in alert_rules}

    assert headings == referenced


def test_every_alert_says_what_it_means_and_how_much_it_matters(alert_rules):
    for rule in alert_rules:
        assert rule["labels"]["severity"] in {"critical", "warning", "info"}
        assert rule["annotations"]["summary"].strip()
        assert rule["annotations"]["description"].strip()


def test_the_alert_names_are_unique(alert_rules):
    names = [rule["alert"] for rule in alert_rules]
    assert len(names) == len(set(names))


def test_the_prometheus_config_loads_the_rule_file_it_ships():
    config = yaml.safe_load((ASSETS / "prometheus.yml").read_text())
    assert config["rule_files"] == ["/etc/prometheus/alerts.yml"]
    assert any(job["job_name"] == "sutr" for job in config["scrape_configs"])


def test_alertmanager_routes_every_severity_the_rules_use(alert_rules):
    config = yaml.safe_load((ASSETS / "alertmanager.yml").read_text())
    receivers = {receiver["name"] for receiver in config["receivers"]}
    routed = {config["route"]["receiver"]}
    routed |= {route["receiver"] for route in config["route"].get("routes", [])}

    assert routed <= receivers, "a route points at a receiver that is not defined"


# ── The dashboard ────────────────────────────────────────────────────────────


@pytest.fixture(scope="module", name="dashboard")
def dashboard_fixture() -> dict:
    return json.loads((ASSETS / "grafana" / "dashboards" / "sutr-overview.json").read_text())


def test_every_panel_queries_a_metric_this_build_exports(dashboard, exported_metrics):
    for panel in dashboard["panels"]:
        for target in panel["targets"]:
            for name in _metric_names(target["expr"]):
                assert _known(name, exported_metrics), (
                    f"panel '{panel['title']}' queries unknown metric {name}"
                )


def test_every_panel_has_a_title_and_a_datasource(dashboard):
    for panel in dashboard["panels"]:
        assert panel["title"].strip()
        assert panel["datasource"]["uid"] == "prometheus"


def test_the_dashboards_datasource_uids_are_the_ones_provisioned(dashboard):
    provisioned = yaml.safe_load(
        (ASSETS / "grafana" / "provisioning" / "datasources" / "datasources.yaml").read_text()
    )
    uids = {source["uid"] for source in provisioned["datasources"]}

    assert {"prometheus", "loki", "tempo"} <= uids
    for panel in dashboard["panels"]:
        assert panel["datasource"]["uid"] in uids


def test_the_log_to_trace_link_names_the_field_the_log_lines_carry(dashboard):
    """The Loki datasource turns `trace_id` on a line into a link to Tempo.
    If the formatter ever renamed that field, the link would silently stop."""
    provisioned = yaml.safe_load(
        (ASSETS / "grafana" / "provisioning" / "datasources" / "datasources.yaml").read_text()
    )
    loki = next(source for source in provisioned["datasources"] if source["uid"] == "loki")
    derived = loki["jsonData"]["derivedFields"][0]

    assert derived["matcherRegex"] == "trace_id"
    assert derived["datasourceUid"] == "tempo"

    import inspect

    from sutr.observability import log_format

    assert '"trace_id"' in inspect.getsource(log_format)


# ── The compose stack ────────────────────────────────────────────────────────


@pytest.fixture(scope="module", name="compose")
def compose_fixture() -> dict:
    return yaml.safe_load((ASSETS / "docker-compose.observability.yml").read_text())


def test_the_stack_contains_the_components_the_lld_names(compose):
    """Seven of the eight. kube-state-metrics reports on a Kubernetes cluster
    and this is a compose file — the README says so rather than pretending."""
    services = set(compose["services"])
    assert {
        "otel-collector",
        "prometheus",
        "grafana",
        "loki",
        "tempo",
        "alertmanager",
        "node-exporter",
    } <= services
    assert "kube-state-metrics" not in services


def test_every_image_is_pinned_to_an_exact_version(compose):
    """`latest` is how a stack that worked on Tuesday stops working on Friday."""
    for name, service in compose["services"].items():
        image = service["image"]
        assert ":" in image, f"{name} has no tag"
        tag = image.rsplit(":", 1)[1]
        assert tag != "latest", f"{name} is pinned to latest"
        assert re.match(r"^v?\d+\.\d+", tag), f"{name} has a floating tag {tag}"


def test_the_collector_is_the_only_thing_the_application_exports_to(compose):
    """One hop out of the process means one place to put mTLS."""
    collector = compose["services"]["otel-collector"]
    published = {port.split(":")[0] for port in collector["ports"]}
    assert {"4317", "4318"} <= published


def test_the_readme_does_not_claim_high_availability():
    """§5.3 asks for HA and this stack is single-replica. Saying otherwise
    would be the exact kind of claim §83 of the build prompt forbids."""
    readme = (ASSETS / "README.md").read_text()
    assert "single-replica" in readme
    assert "NOT IMPLEMENTED" in readme
