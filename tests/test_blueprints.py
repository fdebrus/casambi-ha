"""Validate the shipped automation blueprints.

The blueprints are plain YAML files in the repository, so nothing would
otherwise stop a typo from reaching a user's Home Assistant. These tests run
them through Home Assistant's own blueprint schema.

Note the limit: the test harness pins an older core than the blueprints
require (they use the `event.received` trigger added in 2026.7), so the
blueprint envelope, its inputs and every `!input` reference are validated
here, but the trigger and action bodies are only checked on an instance new
enough to run them.
"""

from pathlib import Path

import pytest

from homeassistant.components.automation.config import AUTOMATION_BLUEPRINT_SCHEMA
from homeassistant.components.blueprint.models import Blueprint
from homeassistant.util.yaml import extract_inputs, parse_yaml

BLUEPRINT_DIR = Path("blueprints/automation/casambi_bt")
BLUEPRINTS = sorted(BLUEPRINT_DIR.glob("*.yaml"))


def _load(path: Path) -> Blueprint:
    """Parse and schema-validate a blueprint the way Home Assistant does."""
    return Blueprint(
        parse_yaml(path.read_text(encoding="utf-8")),
        expected_domain="automation",
        path=str(path),
        schema=AUTOMATION_BLUEPRINT_SCHEMA,
    )


def test_blueprints_are_shipped() -> None:
    """Test that the blueprint directory is not silently empty."""
    assert BLUEPRINTS, f"no blueprints found in {BLUEPRINT_DIR}"


@pytest.mark.parametrize("path", BLUEPRINTS, ids=lambda p: p.name)
def test_blueprint_is_valid(path: Path) -> None:
    """Test that a blueprint parses and matches Home Assistant's schema."""
    blueprint = _load(path)
    assert blueprint.name
    assert blueprint.metadata["description"]


@pytest.mark.parametrize("path", BLUEPRINTS, ids=lambda p: p.name)
def test_blueprint_inputs_are_all_used(path: Path) -> None:
    """Test that every declared input is referenced by the body.

    An input that nothing consumes is a rename or a leftover, and the user
    is asked for a value that goes nowhere. The opposite direction — an
    `!input` with no definition — is rejected by Blueprint() itself.
    """
    blueprint = _load(path)
    unused = set(blueprint.inputs) - extract_inputs(blueprint.data)
    assert not unused, f"inputs declared but never used: {sorted(unused)}"


@pytest.mark.parametrize("path", BLUEPRINTS, ids=lambda p: p.name)
def test_blueprint_declares_min_version(path: Path) -> None:
    """Test that the required core version is declared.

    These blueprints use the `event.received` trigger from 2026.7; without
    min_version an older instance would import them and fail at runtime.
    """
    min_version = _load(path).metadata.get("homeassistant", {}).get("min_version")
    assert min_version, "blueprint does not declare homeassistant.min_version"


@pytest.mark.parametrize("path", BLUEPRINTS, ids=lambda p: p.name)
def test_blueprint_has_source_url(path: Path) -> None:
    """Test that the blueprint can be re-imported for updates.

    Without source_url a user who imported the blueprint cannot pull a fix,
    and the file name must match so the URL keeps resolving after a rename.
    """
    blueprint = _load(path)
    source_url = blueprint.metadata.get("source_url")
    assert source_url, "blueprint has no source_url"
    assert source_url.endswith(f"{BLUEPRINT_DIR.as_posix()}/{path.name}"), (
        f"source_url does not point at this file: {source_url}"
    )


@pytest.mark.parametrize("path", BLUEPRINTS, ids=lambda p: p.name)
def test_blueprint_targets_our_event_entities(path: Path) -> None:
    """Test that button inputs are constrained to this integration.

    A free-text or unfiltered picker would let users select entities the
    blueprint cannot drive.
    """
    blueprint = _load(path)
    # The schema normalises a selector, so check the constraints, not shape.
    entity_selector = blueprint.inputs["button"]["selector"]["entity"]
    assert entity_selector["domain"] == ["event"]
    assert entity_selector["integration"] == "casambi_bt"
