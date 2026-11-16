"""Contrato de aceptación del modelo persistente sin contactar Docker."""

import importlib.util
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "docker/supoclip/probe_local_model.py"


@pytest.fixture
def probe():
    spec = importlib.util.spec_from_file_location("supoclip_model_acceptance", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reinicio_preserva_identidad_y_hora_de_los_otros_servicios(probe):
    before = {
        "success": True,
        "project": "agency-supoclip-trial-nov13",
        "weights": "/opt/model/qwen.gguf",
        "migrate_finished": "2026-11-13T10:20:00Z",
        "containers": {
            role: {"id": role, "started": "2026-11-13T10:30:00Z", "image": "pinned", "restarts": 0}
            for role in probe.ROLES
        },
    }
    after = {
        **before,
        "containers": {role: dict(row) for role, row in before["containers"].items()},
    }
    for role in probe.PAIR:
        after["containers"][role]["started"] = "2026-11-13T10:30:10.123456789Z"
    probe.verify_restart(before, after)
    after["containers"]["backend"]["started"] = "2026-11-13T10:30:09Z"
    with pytest.raises(ValueError, match="backend"):
        probe.verify_restart(before, after)


def test_modelo_rechaza_puerto_publicado_o_namespace_ajeno(probe):
    worker = "a" * 64
    weights = "/opt/model/qwen.gguf"
    good = {
        "user": "1000:1000",
        "entrypoint": ["/usr/local/bin/supoclip-model"],
        "command": ["serve"],
        "namespace": f"container:{worker}",
        "ports": {},
        "read_only": True,
        "cap_drop": ["ALL"],
        "security": ["no-new-privileges:true"],
        "restart": "unless-stopped",
        "health": "healthy",
        "mounts": [
            {
                "Type": "bind",
                "Source": weights,
                "Destination": "/run/supoclip-model/qwen.gguf",
                "RW": False,
            }
        ],
    }
    probe.verify_model(good, worker, weights)
    for field, value in [("ports", {"18963/tcp": [{}]}), ("namespace", "host")]:
        with pytest.raises(ValueError, match="Aislamiento"):
            probe.verify_model({**good, field: value}, worker, weights)


def test_proyeccion_admite_servicios_sin_healthcheck(probe):
    # Docker falla al acceder a un campo omitido con la notación de punto.
    assert 'index .State "Health"' in probe.INSPECT


def test_digest_del_worker_puede_cambiar_sin_aceptar_cambios_en_otros_servicios(probe):
    old_image = "sha256:" + "a" * 64
    worker_image = "sha256:" + "b" * 64
    baseline = {role: {"id": role, "image": old_image} for role in probe.ROLES}
    services = {role: dict(item) for role, item in baseline.items()}
    services["worker"] = {"id": "worker-nuevo", "image": worker_image}
    probe.verify_service_images(baseline, services, worker_image)
    with pytest.raises(ValueError, match="Imagen del worker distinta"):
        probe.verify_service_images(baseline, services)
    with pytest.raises(ValueError, match="Digest esperado del worker inválido"):
        probe.verify_service_images(baseline, services, "worker:latest")
    services["backend"]["image"] = worker_image
    with pytest.raises(ValueError, match="Identidad anterior de backend distinta"):
        probe.verify_service_images(baseline, services, worker_image)
