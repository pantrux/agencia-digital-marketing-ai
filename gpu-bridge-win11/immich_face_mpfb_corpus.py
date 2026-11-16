"""Genera retratos sintéticos de Immich con un paquete local de recursos MPFB verificado."""

import argparse
import hashlib
import importlib
import json
import math
import random
import re
import stat
import struct
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

MPFB_VERSION = "2.0.18"
MPFB_REVISION = "d0a32e57a7f915cb2f2b95410e2117648c7bbb7e"
PACK = "makehuman_system_assets"
PNG_SIZE = 512
SEEDS = (
    145105957,
    684641409,
    200692707,
    1386715852,
    1075313104,
    1542371226,
    1218954265,
    1743801591,
    116852373,
    1743644922,
    1344246229,
    435608765,
    635860973,
    1456110120,
    676307187,
    1478023023,
)
# Condición, acimut de cámara, elevación, acimut de luz principal e intensidad global.
VIEWS = (
    ("frontal", 0, 0, -32, 1.0),
    ("izquierda", -20, 0, -32, 1.0),
    ("derecha", 20, 0, -32, 1.0),
    ("lateral", 0, 0, 13, 1.0),
    ("tenue", 0, 0, -32, 0.5),
    ("elevada", 0, 15, -32, 1.0),
)
CAMERA_DISTANCE = 0.88
CAMERA_LENS = 85
CAMERA_SENSOR = 36
FACE_DETAIL_SECTIONS = {
    "cheek",
    "chin",
    "ears",
    "eyebrows",
    "eyes",
    "forehead",
    "head",
    "mouth",
    "nose",
}


def require(condition, detail):
    if not condition:
        raise ValueError(detail)


def sha_file(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def read_seed_manifest(path, expected_sha256):
    require(
        re.fullmatch(r"[0-9a-f]{64}", expected_sha256),
        "Indica el SHA-256 de las semillas en minúsculas",
    )
    require(
        stat.S_ISREG(path.lstat().st_mode), "El manifiesto de semillas debe ser un archivo regular"
    )
    max_bytes = 16 * 1024
    with path.open("rb") as handle:
        content = handle.read(max_bytes + 1)
    require(len(content) <= max_bytes, "El manifiesto de semillas excede 16 KiB")
    require(
        hashlib.sha256(content).hexdigest() == expected_sha256,
        "SHA-256 del manifiesto de semillas incorrecto",
    )

    def unique_keys(pairs):
        fields = dict(pairs)
        require(len(fields) == len(pairs), "El manifiesto de semillas contiene claves repetidas")
        return fields

    manifest = json.loads(content.decode("utf-8"), object_pairs_hook=unique_keys)
    require(
        isinstance(manifest, dict)
        and set(manifest) == {"schema", "cohort", "calibration", "validation"}
        and type(manifest["schema"]) is int
        and manifest["schema"] == 1,
        "Esquema del manifiesto de semillas inválido",
    )
    cohort = manifest["cohort"]
    require(
        isinstance(cohort, str)
        and re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", cohort)
        and len(cohort) <= 24,
        "Nombre de cohorte inválido",
    )
    seen = set()
    identities = []
    for split in ("calibration", "validation"):
        seeds = manifest[split]
        require(
            isinstance(seeds, list) and 2 <= len(seeds) <= 16,
            f"La partición {split} requiere de 2 a 16 semillas",
        )
        for seed in seeds:
            require(
                type(seed) is int and 0 <= seed < 2**32,
                "Las semillas deben ser enteras de 32 bits sin signo",
            )
            require(seed not in SEEDS, "El manifiesto contiene semillas históricas")
            require(seed not in seen, "El manifiesto contiene semillas repetidas")
            seen.add(seed)
            identities.append((f"mh-{cohort}-{len(identities) + 1:03d}", seed, split))
    return cohort, tuple(identities)


def read_arguments(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--count", type=int, choices=(2, 16), help="Tamaño histórico del corpus, 2 por defecto"
    )
    selection.add_argument(
        "--seed-manifest", type=Path, help="Manifiesto JSON con una cohorte y dos particiones"
    )
    parser.add_argument(
        "--seed-manifest-sha256", help="SHA-256 de los bytes del manifiesto de semillas"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--assets-zip", type=Path, required=True)
    parser.add_argument("--assets-sha256", required=True)
    parser.add_argument("--addon", default="bl_ext.user_default.mpfb")
    parser.add_argument("--save-scenes", action="store_true")
    args = parser.parse_args(argv)
    require(
        re.fullmatch(r"[0-9a-f]{64}", args.assets_sha256), "Indica el SHA-256 del ZIP en minúsculas"
    )
    require(
        re.fullmatch(r"bl_ext\.[A-Za-z_][A-Za-z_0-9]*\.mpfb", args.addon), "Módulo MPFB inválido"
    )
    require(
        (args.seed_manifest is None) == (args.seed_manifest_sha256 is None),
        "Indica --seed-manifest y --seed-manifest-sha256 juntos",
    )
    if args.seed_manifest is None:
        args.count = args.count or 2
        args.identities = tuple(
            (f"mh-{index:03d}", seed, "calibration" if index <= 8 else "validation")
            for index, seed in enumerate(SEEDS[: args.count], start=1)
        )
    else:
        args.cohort, args.identities = read_seed_manifest(
            args.seed_manifest, args.seed_manifest_sha256
        )
    return args


def extract_core_assets(path, expected_sha256, root):
    with path.open("rb") as handle:
        require(
            hashlib.file_digest(handle, "sha256").hexdigest() == expected_sha256,
            "SHA-256 del ZIP incorrecto",
        )
        handle.seek(0)
        with zipfile.ZipFile(handle) as archive:
            paths = set()
            for item in archive.infolist():
                name = item.orig_filename.rstrip("/")
                parts = name.split("/")
                require(
                    not PurePosixPath(name).is_absolute()
                    and all(
                        part not in {"", ".", ".."}
                        and not PureWindowsPath(part).drive
                        and not part.endswith((".", " "))
                        and not any(char in part for char in "\\:\x00")
                        for part in parts
                    )
                    and stat.S_IFMT(item.external_attr >> 16) in (0, stat.S_IFREG, stat.S_IFDIR)
                    and name.casefold() not in paths,
                    f"Entrada de ZIP inválida: {name}",
                )
                paths.add(name.casefold())
            require(
                f"packs/{PACK}.json" in paths,
                "El ZIP no contiene los recursos de sistema esperados",
            )
            archive.extractall(root)


def load_addon(bpy, name):
    if name not in bpy.context.preferences.addons:
        require(
            bpy.ops.preferences.addon_enable(module=name) == {"FINISHED"}, "No se pudo activar MPFB"
        )
    addon = importlib.import_module(name)
    manifest_path = Path(addon.__file__).with_name("blender_manifest.toml")
    with manifest_path.open("rb") as handle:
        manifest = tomllib.load(handle)
    require(
        manifest["id"] == "mpfb" and manifest["version"] == MPFB_VERSION, "Se requiere MPFB 2.0.18"
    )
    return sha_file(manifest_path)


def setup_scene(bpy):
    scene = bpy.context.scene
    require(not bpy.data.filepath, "Abre un Blender sin archivo .blend de entrada")
    require(
        set(bpy.data.objects.keys()) <= {"Cube", "Light", "Camera"},
        "Usa una sesión de Blender con escena de fábrica",
    )
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)

    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 32
    scene.cycles.use_adaptive_sampling = False
    scene.cycles.use_denoising = True
    scene.cycles.seed = 431
    scene.render.resolution_x = PNG_SIZE
    scene.render.resolution_y = PNG_SIZE
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.image_settings.color_depth = "8"
    scene.render.film_transparent = False
    scene.render.threads_mode = "FIXED"
    scene.render.threads = 4
    scene.view_settings.view_transform = "AgX"

    scene.world = bpy.data.worlds.new("Portrait background")
    scene.world.use_nodes = True
    background = scene.world.node_tree.nodes.get("Background")
    background.inputs["Color"].default_value = (0.18, 0.18, 0.18, 1)

    data = bpy.data.cameras.new("Portrait camera")
    data.lens = CAMERA_LENS
    data.sensor_width = CAMERA_SENSOR
    data.sensor_fit = "HORIZONTAL"
    data.clip_start = 0.02
    camera = bpy.data.objects.new("Portrait camera", data)
    scene.collection.objects.link(camera)
    scene.camera = camera

    lights = []
    for name, size in (("Key", 0.70), ("Fill", 0.95), ("Rim", 0.65)):
        data = bpy.data.lights.new(name, "AREA")
        data.shape = "DISK"
        data.size = size
        light = bpy.data.objects.new(name, data)
        scene.collection.objects.link(light)
        lights.append(light)
    return scene, camera, lights, background


def discovery_for_core(root, builder):
    def candidates(part, pattern):
        paths = sorted((root / part).rglob(pattern))
        require(paths, f"El ZIP no contiene recursos para {part}")
        return [{"name": path.stem, "path": str(path), "pack": PACK} for path in paths]

    return {
        "skin_candidates": candidates("skins", "*.mhmat"),
        "bodypart_candidates": {part: candidates(part, "*.mhclo") for part in ("eyebrows", "hair")},
        "detail_sections": builder._parse_detail_sections(),
    }


def configure_character(spec):
    spec["creation"].update(
        rig="NONE",
        scale_factor="METER",
        mask_helpers=True,
        add_subdiv_modifier=True,
        subdiv_render_levels=1,
    )
    phenotype = spec["phenotype"]
    phenotype["discrete_age"] = False
    phenotype["attributes"]["age"].update(include=True, neutral=0.54, deviation=0.17)
    spec["details"].update(enabled=True, symmetry=True)
    for name, section in spec["details"]["sections"].items():
        if name in FACE_DETAIL_SECTIONS:
            section.update(min=1, max=3, deviation=0.38)
        else:
            section.update(min=0, max=0)

    assets = spec["assets"]
    assets["skin"].update(
        enabled=True,
        match_gender=True,
        match_age=False,
        match_race=True,
        fallback=True,
        pack=PACK,
        skin_type="MAKESKIN",
    )
    # Los ojos se añaden explícitamente desde el paquete verificado.
    assets["eyes"]["mode"] = "DONOTADD"
    assets["hair"].update(
        enabled=True, match_gender=True, fallback=True, pack=PACK, randomize_alt_materials=False
    )
    for part in ("eyebrows", "eyelashes", "teeth", "tongue"):
        assets[part].update(enabled=False)
    assets["eyebrows"].update(enabled=True, pack=PACK)
    for slot in assets["clothes"].values():
        slot["enabled"] = False


def vertex_positions(obj, graph):
    evaluated = obj.evaluated_get(graph)
    matrix = evaluated.matrix_world
    return [matrix @ vertex.co for vertex in evaluated.data.vertices]


def face_frame(body, eyes, graph, vector):
    body_group = body.vertex_groups.get("body")
    require(body_group is not None, "MPFB no devolvió el grupo de geometría corporal")
    evaluated = body.evaluated_get(graph)
    points = [
        evaluated.matrix_world @ vertex.co
        for vertex in evaluated.data.vertices
        if any(group.group == body_group.index for group in vertex.groups)
    ]
    eye_points = vertex_positions(eyes, graph)
    require(points and eye_points, "Malla sin vértices de cuerpo u ojos")
    low = min(v.z for v in points)
    high = max(v.z for v in points)
    height = high - low
    center = sum(eye_points, vector((0, 0, 0))) / len(eye_points)
    require(
        1.15 <= height <= 2.30 and 0.85 < (center.z - low) / height < 0.98,
        "Escala o altura de ojos inesperada",
    )
    head_y = [point.y for point in points if abs(point.z - center.z) <= 0.025 * height]
    require(head_y, "No se encontraron vértices a la altura de los ojos")
    front_y = -1 if center.y < (min(head_y) + max(head_y)) / 2 else 1
    center.z -= 0.025 * height
    return center, front_y


def aim(obj, center, front_y, distance, azimuth, elevation, vector):
    yaw, pitch = math.radians(azimuth), math.radians(elevation)
    direction = vector(
        (
            math.sin(yaw) * math.cos(pitch),
            front_y * math.cos(yaw) * math.cos(pitch),
            math.sin(pitch),
        )
    )
    obj.location = center + distance * direction
    obj.rotation_euler = (center - obj.location).to_track_quat("-Z", "Y").to_euler()


def position_view(camera, lights, background, center, front_y, view, vector):
    _, azimuth, elevation, key_azimuth, intensity = view
    aim(camera, center, front_y, CAMERA_DISTANCE, azimuth, elevation, vector)
    background.inputs["Strength"].default_value = 0.42 * intensity
    for light, (yaw, pitch, distance, power) in zip(
        lights, ((key_azimuth, 24, 1.10, 105), (34, 10, 1.15, 60), (154, 35, 1.10, 75)), strict=True
    ):
        aim(light, center, front_y, distance, yaw, pitch, vector)
        light.data.energy = power * intensity


def geometry_sha256(body, graph):
    digest = hashlib.sha256()
    for vertex in body.evaluated_get(graph).data.vertices:
        digest.update(struct.pack("<3f", *vertex.co))
    return digest.hexdigest()


def material_images(tree):
    for node in tree.nodes:
        if node.type == "TEX_IMAGE" and node.image is not None:
            yield node.image
        if node.type == "GROUP" and node.node_tree is not None:
            yield from material_images(node.node_tree)


def texture_receipts(bpy, body, asset_root, mpfb_data):
    receipts = {}
    materials = {
        slot.material
        for obj in [body, *body.children_recursive]
        for slot in obj.material_slots
        if slot.material is not None and slot.material.node_tree is not None
    }
    images = {image for material in materials for image in material_images(material.node_tree)}
    for image in images:
        if image.source != "FILE":
            continue
        path = Path(bpy.path.abspath(image.filepath)).resolve(strict=True)
        for label, root in (("asset_pack", asset_root), ("mpfb_core", mpfb_data)):
            if path.is_relative_to(root):
                receipts[f"{label}/{path.relative_to(root).as_posix()}"] = sha_file(path)
                break
        else:
            raise ValueError(f"Textura ajena al paquete acreditado: {path.name}")
    require(receipts, "El personaje no tiene texturas acreditadas")
    return receipts


def check_png(path):
    with path.open("rb") as handle:
        header = handle.read(29)
    require(
        header[:16] == b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR"
        and struct.unpack(">IIBB", header[16:26]) == (PNG_SIZE, PNG_SIZE, 8, 2),
        f"PNG RGB inesperado: {path.name}",
    )


def write_json(path, payload):
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )


def generate(bpy, vector, args, root, output):
    addon_manifest_sha = load_addon(bpy, args.addon)
    services = importlib.import_module(f"{args.addon}.services")
    asset_settings = importlib.import_module(
        f"{args.addon}.ui.apply_assets.assetlibrary.assetsettingspanel"
    )
    randomize = f"{args.addon}.ui.new_human.randomize"
    builder = importlib.import_module(f"{randomize}.characterbuilder")
    properties = importlib.import_module(f"{randomize}.randomizeproperties")
    scene, camera, lights, background = setup_scene(bpy)
    asset_settings.ASSET_SETTINGS_PROPERTIES.set_value(
        "second_root", str(root), entity_reference=scene
    )
    spec = properties.scene_to_spec(scene)
    configure_character(spec)
    discovery = discovery_for_core(root, builder)
    mpfb_data = Path(services.LocationService.get_mpfb_data()).resolve(strict=True)
    eye_asset = root / "eyes" / "high-poly" / "high-poly.mhclo"
    require(eye_asset.is_file(), "Falta eyes/high-poly/high-poly.mhclo en el paquete")
    outfits = {
        sex: list((root / "clothes").rglob(f"{sex}_casualsuit01.mhclo"))
        for sex in ("female", "male")
    }
    require(all(len(paths) == 1 for paths in outfits.values()), "Falta ropa básica del paquete CC0")
    records, identities = [], []

    for identity, seed, split in args.identities:
        warnings = []

        def report(level, message, warning_sink=warnings):
            if level & {"WARNING", "ERROR"}:
                warning_sink.append(message)

        rng = random.Random(seed)
        macros = services.RandomizationService.randomize_macro_info_dict(spec, rng)
        body = builder.build_character(spec, macros, rng, report, discovery)
        require(not warnings, f"{identity}: {warnings}")
        body.name = f"GEO-{identity}"
        eyes = services.HumanService.add_mhclo_asset(
            str(eye_asset), body, asset_type="eyes", subdiv_levels=1, material_type="MAKESKIN"
        )
        require(eyes is not None, f"{identity}: MPFB no añadió ojos")
        bpy.context.view_layer.update()
        graph = bpy.context.evaluated_depsgraph_get()
        center, front_y = face_frame(body, eyes, graph, vector)
        mesh_sha = geometry_sha256(body, graph)
        sex = "female" if macros["gender"] < 0.5 else "male"
        services.HumanService.add_mhclo_asset(str(outfits[sex][0]), body)
        human = json.loads(services.HumanService.serialize_to_json_string(body, save_clothes=True))
        require(
            all(human[key] for key in ("skin_mhmat", "eyes", "hair", "eyebrows", "clothes")),
            f"{identity}: MPFB no registró todos los recursos",
        )
        recipe = output / "models" / f"{identity}.json"
        write_json(recipe, human)
        metadata = {
            "identity": identity,
            "seed": seed,
            "mesh_sha256": mesh_sha,
            "model_path": recipe.relative_to(output).as_posix(),
            "model_sha256": sha_file(recipe),
            "target": [round(value, 7) for value in center],
            "front_y": front_y,
            "textures_sha256": texture_receipts(bpy, body, root, mpfb_data),
        }

        for view in VIEWS:
            condition = view[0]
            fixture = f"{identity}_{condition}"
            image = output / "images" / (fixture + ".png")
            position_view(camera, lights, background, center, front_y, view, vector)
            scene.render.filepath = str(image)
            require(
                bpy.ops.render.render(write_still=True) == {"FINISHED"},
                f"Render fallido: {fixture}",
            )
            check_png(image)
            digest = sha_file(image)
            records.append(
                {
                    "id": fixture,
                    "identity": identity,
                    "split": split,
                    "condition": condition,
                    "path": image.relative_to(output).as_posix(),
                    "original_sha256": digest,
                    "prepared_sha256": digest,
                    "width": PNG_SIZE,
                    "height": PNG_SIZE,
                    "expected_faces": 1,
                }
            )

        if args.save_scenes:
            scene_path = recipe.with_suffix(".blend")
            position_view(camera, lights, background, center, front_y, VIEWS[0], vector)
            bpy.ops.file.pack_all()
            asset_settings.ASSET_SETTINGS_PROPERTIES.set_value(
                "second_root", "", entity_reference=scene
            )
            result = bpy.ops.wm.save_as_mainfile(
                filepath=str(scene_path), relative_remap=False, copy=True
            )
            asset_settings.ASSET_SETTINGS_PROPERTIES.set_value(
                "second_root", str(root), entity_reference=scene
            )
            require(result == {"FINISHED"}, f"Escena no guardada: {identity}")
            metadata.update(
                scene_path=scene_path.relative_to(output).as_posix(),
                scene_sha256=sha_file(scene_path),
            )
        identities.append(metadata)
        for obj in [*body.children_recursive, body]:
            bpy.data.objects.remove(obj, do_unlink=True)

    count = len(args.identities)
    require(
        len({r["prepared_sha256"] for r in records}) == count * len(VIEWS),
        "El corpus contiene PNG duplicados",
    )
    require(
        len({r["mesh_sha256"] for r in identities}) == count, "El corpus contiene mallas duplicadas"
    )
    manifest = {
        "schema": 1,
        "synthetic": True,
        "asset_license": "CC0-1.0",
        "asset_pack": PACK,
        "asset_pack_sha256": args.assets_sha256,
        "mpfb_version": MPFB_VERSION,
        "mpfb_reference_commit": MPFB_REVISION,
        "mpfb_manifest_sha256": addon_manifest_sha,
        "blender_version": bpy.app.version_string,
        "blender_build_hash": bpy.app.build_hash.decode("ascii"),
        "renderer": "Cycles CPU",
        "camera": {
            "lens_mm": CAMERA_LENS,
            "sensor_mm": CAMERA_SENSOR,
            "distance_m": CAMERA_DISTANCE,
        },
        "views": [
            {
                "condition": condition,
                "yaw_deg": yaw,
                "pitch_deg": pitch,
                "key_yaw_deg": key_yaw,
                "light_scale": scale,
            }
            for condition, yaw, pitch, key_yaw, scale in VIEWS
        ],
        "mpfb_spec": spec,
        "identities": identities,
        "fixtures": records,
        "script_sha256": sha_file(Path(__file__)),
    }
    if args.seed_manifest is not None:
        manifest.update(
            seed_manifest_cohort=args.cohort, seed_manifest_sha256=args.seed_manifest_sha256
        )
    write_json(output / "fixtures.json", manifest)
    print(
        json.dumps(
            {
                "identities": len(identities),
                "fixtures": len(records),
                "manifest_sha256": sha_file(output / "fixtures.json"),
            },
            sort_keys=True,
        )
    )


def main():
    args = read_arguments(
        sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else sys.argv[1:]
    )
    import bpy
    from mathutils import Vector

    require(bpy.app.background, "Ejecuta Blender con --background")
    require(not bpy.app.online_access, "Ejecuta Blender con --offline-mode")
    output = args.output.resolve()
    require(not output.exists(), "La salida debe ser un directorio nuevo")
    with tempfile.TemporaryDirectory(prefix="immich-mpfb-assets-") as temp:
        root = Path(temp).resolve()
        extract_core_assets(args.assets_zip, args.assets_sha256, root)
        output.mkdir()
        (output / "images").mkdir()
        (output / "models").mkdir()
        generate(bpy, Vector, args, root, output)


if __name__ == "__main__":
    main()
