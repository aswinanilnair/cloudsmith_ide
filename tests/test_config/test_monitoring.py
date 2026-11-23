"""The monitoring configuration (monitoring/) is consistent with itself, with the scripts that
create it, and with the log lines it reads. It can only really be seen on GCP, so these checks
catch mistakes before a deploy."""
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
MONITORING = ROOT / "monitoring"
API_SOURCE = (ROOT / "services" / "api" / "app" / "main.py").read_text()


# Reads the log-based metric names and config files from scripts/common.sh.
def log_metrics() -> dict:
    line = re.search(r'^LOG_METRICS="([^"]+)"', (ROOT / "scripts" / "common.sh").read_text(), re.M).group(1)
    return dict(entry.split(":") for entry in line.split())


# Loads the monitoring dashboard definition.
def dashboard() -> dict:
    return json.loads((MONITORING / "dashboard.json").read_text())


# Every log-based metric's config file exists and is valid.
def test_every_log_metric_file_exists_and_is_valid():
    for name, file in log_metrics().items():
        config = yaml.safe_load((MONITORING / file).read_text())
        assert config["filter"] and config["metricDescriptor"]["metricKind"] == "DELTA", name


# The dashboard only uses log-based metrics that setup.sh creates.
def test_the_dashboard_only_uses_log_metrics_that_setup_creates():
    used = set(re.findall(r'logging.googleapis.com/user/(\w+)', json.dumps(dashboard())))
    assert used == set(log_metrics())


# Dashboard tiles fit the grid without overlapping.
def test_dashboard_tiles_fit_the_grid_without_overlapping():
    layout = dashboard()["mosaicLayout"]
    taken = set()
    for tile in layout["tiles"]:
        assert tile["xPos"] + tile["width"] <= layout["columns"], tile["widget"]["title"]
        cells = {(x, y) for x in range(tile["xPos"], tile["xPos"] + tile["width"])
                 for y in range(tile["yPos"], tile["yPos"] + tile["height"])}
        assert not cells & taken, f"{tile['widget']['title']} overlaps another tile"
        taken |= cells


# Each log-based metric reads a log line and fields the API really writes.
@pytest.mark.parametrize("name", sorted(log_metrics()))
def test_log_metrics_read_log_lines_and_fields_the_api_writes(name):
    config = yaml.safe_load((MONITORING / log_metrics()[name]).read_text())
    message = re.search(r'jsonPayload.message="([^"]+)"', config["filter"]).group(1)
    assert f'log("{message}"' in API_SOURCE, f"the API never logs {message!r}"
    fields = re.findall(r"EXTRACT\(jsonPayload\.(\w+)\)",
                        json.dumps([config.get("valueExtractor", ""), config.get("labelExtractors", {})]))
    call = API_SOURCE[API_SOURCE.index(f'log("{message}"'):]
    call = call[:call.index("\n\n")]  # the log call and the lines just after it
    for field in fields:
        assert f"{field}=" in call, f"{message!r} lines have no {field!r} field"


# Metric filters name the API service set in scripts/common.sh.
def test_metric_filters_name_the_api_service_from_common_sh():
    service = re.search(r"^API_SERVICE=(\S+)", (ROOT / "scripts" / "common.sh").read_text(), re.M).group(1)
    for file in log_metrics().values():
        assert f'service_name="{service}"' in (MONITORING / file).read_text()
    assert f'\\"{service}\\"' in json.dumps(dashboard())
