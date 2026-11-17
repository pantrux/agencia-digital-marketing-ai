import json
from array import array
from pathlib import Path
from random import Random

import pytest


@pytest.fixture
def cut(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "docker" / "n8n"))
    import production_resolve as resolve

    order_id = "b9bb94eb-fd77-4b73-8639-4d20da96a17d"
    slug = "job-" + order_id.replace("-", "")
    asset_id = "6f6eb550-a53f-4655-875c-b072ed552e9d"
    video = f"proyectos/{slug}/01_raw/{asset_id}.mp4"
    audio = f"proyectos/{slug}/03_assets/intake-source.wav"
    ingesta = {
        "order": {
            "order_id": order_id,
            "plan_id": "ilimitado",
            "media": {
                "project_slug": slug,
                "asset_id": asset_id,
                "extension": "mp4",
                "sha256": "a" * 64,
            },
        },
        "raw": video,
        "audio": {"path": audio},
        "duration_seconds": 45,
    }
    props = {
        "video_path": video,
        "audio_path": audio,
        "duration_seconds": 30,
        "scenes": [{"source_start_frame": 90, "source_end_frame": 990}],
        "ad": {
            "soundtrack": resolve.AD_MUSIC_ID,
            "captions": [{"start_frame": 1, "end_frame": 30, "text": "Frase de prueba"}],
        },
    }
    direction = {
        "order_id": order_id,
        "source_sha256": "a" * 64,
        "escaleta": {
            "estado": "lista_para_stills",
            "render": {
                "project_id": slug,
                "plan": "Ilimitado",
                "composition": "AgencyVertical",
                "format": "mp4",
                "props": props,
            },
        },
    }
    return resolve, ingesta, direction, props


def test_traspaso_conserva_fotogramas_y_rechaza_corte_alterado(cut):
    resolve, intake, direction, props = cut
    assert resolve.validated_cut(intake, direction) == (intake["order"], props, 1350)
    assert resolve.srt_time(1) == "00:00:00,033"
    assert resolve.srt_time(900) == "00:00:30,000"
    props["scenes"][0]["source_end_frame"] = 991
    with pytest.raises(ValueError, match="900 fotogramas"):
        resolve.validated_cut(intake, direction)


def test_rechaza_otro_proyecto_y_subtitulos_superpuestos(cut):
    resolve, intake, direction, props = cut
    path = props["video_path"]
    props["video_path"] = "proyectos/otro/01_raw/video.mp4"
    with pytest.raises(ValueError, match="escaleta acreditada"):
        resolve.validated_cut(intake, direction)
    props["video_path"] = path
    props["ad"]["captions"].append({"start_frame": 29, "end_frame": 31, "text": "Segunda frase"})
    with pytest.raises(ValueError, match="Subtítulo inválido"):
        resolve.validated_cut(intake, direction)


def test_rechaza_insumo_modificado_despues_de_preparar(cut, tmp_path):
    import hashlib

    resolve, intake, direction, _ = cut
    for name in resolve.PACKAGE_FILES:
        (tmp_path / name).write_bytes(b"medio de ensayo")
    metadata = {name: resolve.inspect_file(tmp_path / name) for name in resolve.PACKAGE_FILES}
    order = intake["order"]
    digest = hashlib.sha256(resolve.canonical(direction)).hexdigest()
    (tmp_path / "traspaso.json").write_text(
        json.dumps(
            {
                "order_id": order["order_id"],
                "direction_sha256": digest,
                "soundtrack_sha256": resolve.MUSIC_SHA256,
                "files": metadata,
            }
        ),
        encoding="utf-8",
    )
    assert resolve.verify_package(order, direction, tmp_path) == digest
    (tmp_path / "voz-original.wav").write_bytes(b"voz reemplazada")
    with pytest.raises(ValueError, match="insumos de edición cambiaron"):
        resolve.verify_package(order, direction, tmp_path)


def test_reconoce_musica_desplazada_y_rechaza_otra_pista(cut):
    from production_elena import matching_music_window

    generator = Random(17)
    reference = array("h", (generator.randint(-20000, 20000) for _ in range(16000)))
    segment = array("h", (sample // 4 for sample in reference[9000:13000]))
    assert matching_music_window(segment, reference)
    assert not matching_music_window(array("h", [0] * 4000), reference)
    other = array("h", (generator.randint(-20000, 20000) for _ in range(4000)))
    assert not matching_music_window(other, reference)
