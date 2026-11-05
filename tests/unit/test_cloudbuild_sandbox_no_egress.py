"""S6 network lock-down of both sandbox services (UNTRUSTED_CONTENT_SANDBOX_RFC §4.5): the deploy scripts pass the
network flags from env, refuse empty values, and the build file and the Makefile carry the two values through."""
import os
import subprocess
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_INFRA = _ROOT / "scripts" / "infrastructure"
_BUILD = yaml.safe_load((_ROOT / "cloudbuild-dev.yaml").read_text())
_MAKEFILE = (_ROOT / "Makefile").read_text()

_FAMILIES = {
    "light": ("cloudbuild_sandbox_light.sh", "SANDBOX_LIGHT_SA", "sandbox-light-deploy"),
    "render": ("cloudbuild_sandbox_render.sh", "SANDBOX_RENDER_SA", "sandbox-deploy"),
}
_NET = "net-placeholder"
_SUBNET = "subnet-placeholder"


def _run(family, tmp_path, drop=(), **env_over):
    script, sa_var, _ = _FAMILIES[family]
    stub = tmp_path / "gcloud"
    stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$GCLOUD_ARGS_OUT"\n')
    stub.chmod(0o755)
    out = tmp_path / "args.txt"
    env = {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "PROJECT_ID": "placeholder-project",
        sa_var: "alek-sandbox-x@placeholder.example",
        "SANDBOX_VPC_NETWORK": _NET,
        "SANDBOX_VPC_SUBNET": _SUBNET,
        "GCLOUD_ARGS_OUT": str(out),
    }
    env.update(env_over)
    for key in drop:
        del env[key]
    proc = subprocess.run(["bash", str(_INFRA / script), "deploy"], env=env, capture_output=True, text=True)
    args = out.read_text().splitlines() if out.exists() else []
    return proc, args


@pytest.mark.parametrize("family", list(_FAMILIES))
def test_deploy_passes_network_flags_from_env(family, tmp_path):
    proc, args = _run(family, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert f"--network={_NET}" in args
    assert f"--subnet={_SUBNET}" in args
    assert "--vpc-egress=all-traffic" in args


@pytest.mark.parametrize("family", list(_FAMILIES))
@pytest.mark.parametrize("name", ["SANDBOX_VPC_NETWORK", "SANDBOX_VPC_SUBNET"])
def test_deploy_refuses_empty_network_value(family, name, tmp_path):
    proc, args = _run(family, tmp_path, **{name: ""})
    assert proc.returncode != 0
    assert name in proc.stderr
    assert args == []


@pytest.mark.parametrize("family", list(_FAMILIES))
@pytest.mark.parametrize("name", ["SANDBOX_VPC_NETWORK", "SANDBOX_VPC_SUBNET"])
def test_deploy_refuses_unset_network_value(family, name, tmp_path):
    proc, args = _run(family, tmp_path, drop=(name,))
    assert proc.returncode != 0
    assert name in proc.stderr
    assert args == []


@pytest.mark.parametrize("family", list(_FAMILIES))
def test_empty_sa_still_skips_before_the_network_check(family, tmp_path):
    _, sa_var, _ = _FAMILIES[family]
    proc, args = _run(family, tmp_path, **{sa_var: "", "SANDBOX_VPC_NETWORK": "", "SANDBOX_VPC_SUBNET": ""})
    assert proc.returncode == 0
    assert args == []


@pytest.mark.parametrize("family", list(_FAMILIES))
def test_deploy_step_env_carries_the_substitutions(family):
    step = next(s for s in _BUILD["steps"] if s.get("id") == _FAMILIES[family][2])
    assert "SANDBOX_VPC_NETWORK=$_SANDBOX_VPC_NETWORK" in step["env"]
    assert "SANDBOX_VPC_SUBNET=$_SANDBOX_VPC_SUBNET" in step["env"]


def test_substitutions_default_to_empty():
    subs = _BUILD["substitutions"]
    assert subs["_SANDBOX_VPC_NETWORK"] == ""
    assert subs["_SANDBOX_VPC_SUBNET"] == ""


def test_makefile_passes_the_substitutions_from_env_keys():
    assert "_SANDBOX_VPC_NETWORK = $(SANDBOX_VPC_NETWORK_DEV)" in _MAKEFILE
    assert "_SANDBOX_VPC_SUBNET = $(SANDBOX_VPC_SUBNET_DEV)" in _MAKEFILE
    assert "_SANDBOX_VPC_NETWORK=$(_SANDBOX_VPC_NETWORK)" in _MAKEFILE
    assert "_SANDBOX_VPC_SUBNET=$(_SANDBOX_VPC_SUBNET)" in _MAKEFILE


def test_makefile_deploy_refuses_empty_network_keys():
    guard = next(ln for ln in _MAKEFILE.splitlines() if "-z" in ln and "SANDBOX_VPC_NETWORK_DEV" in ln)
    assert "-z \"$(SANDBOX_VPC_SUBNET_DEV)\"" in guard
