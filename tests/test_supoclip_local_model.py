"""Contratos del modelo local y su Compose, sin red ni Docker."""

import importlib.util
import io
import tarfile
from pathlib import Path

import pytest
import yaml

SOURCE = Path(__file__).resolve().parents[1] / "docker/supoclip"
SPEC = importlib.util.spec_from_file_location(
    "prepare_supoclip_local_model", SOURCE / "prepare_local_model.py"
)
PREPARE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREPARE)


def test_modelo_comparte_solo_loopback_del_worker():
    services = yaml.safe_load((SOURCE / "compose.local-model.yml").read_text("utf8"))["services"]
    assert set(services) == {"model", "worker"}
    model = services["model"]
    assert model["network_mode"] == "service:worker"
    assert not {"ports", "networks", "privileged", "devices"} & set(model)
    assert model["read_only"] and model["cap_drop"] == ["ALL"]
    assert len(model["volumes"]) == 1 and model["volumes"][0]["read_only"]
    assert model["command"] == ["serve"]
    assert "network_mode" not in services["worker"]
    env = services["worker"]["environment"]
    assert env["LLM"] == env["RERANK_LLM"] == "ollama:qwen-supoclip"
    assert env["OLLAMA_BASE_URL"] == "http://127.0.0.1:18963/v1"
    launch = (SOURCE / "launch-local-model.sh").read_text("utf8")
    assert "--host 127.0.0.1 --port 18963" in launch
    assert "--alias qwen-supoclip --no-webui --jinja" in launch


def build_engine(path, link_target):
    elf = b"\x7fELF\x02\x01" + b"\x00" * 12 + b"\x3e\x00"
    with tarfile.open(path, "w:gz") as archive:
        for name, data in (
            ("llama-server", elf),
            ("LICENSE", b"Test"),
            ("libllama-server-impl.so", elf),
            ("libggml-cpu-x64.so", elf),
            ("llama-other", b"excluded"),
        ):
            info = tarfile.TarInfo(PREPARE.ENGINE_PREFIX + name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo(PREPARE.ENGINE_PREFIX + "libexample.so")
        link.type = tarfile.SYMTYPE
        link.linkname = link_target
        archive.addfile(link)


def test_proyecta_solamente_servidor_y_bibliotecas(tmp_path):
    original = tmp_path / "upstream.tar.gz"
    packaged = tmp_path / "engine.tar.gz"
    build_engine(original, "libllama-server-impl.so")
    assert PREPARE.copy_engine(original, packaged) == 5
    with tarfile.open(packaged) as archive:
        assert archive.getmember("llama-server").mode == 0o555
        assert archive.getmember("libexample.so").linkname == "libllama-server-impl.so"
        assert "llama-other" not in archive.getnames()


def test_rechaza_enlace_que_escapa_del_motor(tmp_path):
    original = tmp_path / "upstream.tar.gz"
    build_engine(original, "../libllama-server-impl.so")
    with pytest.raises(ValueError, match="Enlace de motor inválido"):
        PREPARE.copy_engine(original, tmp_path / "engine.tar.gz")
