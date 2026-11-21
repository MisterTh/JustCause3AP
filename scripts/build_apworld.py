import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "worlds" / "justcause3"
DEFINITIONS_PATH = PACKAGE_DIR / "archi_defs.py"
OUTPUT_PATH = ROOT / "justcause3.apworld"
APWORLD_CONTAINER_VERSION = 7


def main() -> None:
    if not (PACKAGE_DIR / "__init__.py").is_file():
        raise FileNotFoundError(f"World package not found: {PACKAGE_DIR}")
    if not DEFINITIONS_PATH.is_file():
        raise FileNotFoundError(
            f"Generated game definitions not found: {DEFINITIONS_PATH}. "
            "Run scripts/get_checks.py before building the APWorld."
        )

    manifest_path = PACKAGE_DIR / "archipelago.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        version=APWORLD_CONTAINER_VERSION,
        compatible_version=APWORLD_CONTAINER_VERSION,
    )

    with ZipFile(OUTPUT_PATH, "w", compression=ZIP_DEFLATED) as archive:
        for path in sorted(PACKAGE_DIR.rglob("*")):
            if (
                path.is_file()
                and path != manifest_path
                and "__pycache__" not in path.parts
            ):
                archive.write(
                    path,
                    (Path(PACKAGE_DIR.name) / path.relative_to(PACKAGE_DIR)).as_posix(),
                )
        archive.writestr(
            f"{PACKAGE_DIR.name}/archipelago.json",
            json.dumps(manifest),
        )

    with ZipFile(OUTPUT_PATH) as archive:
        definitions_name = f"{PACKAGE_DIR.name}/archi_defs.py"
        if definitions_name not in archive.namelist():
            raise RuntimeError(f"Built APWorld is missing {definitions_name}")

    print(f"Built {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
