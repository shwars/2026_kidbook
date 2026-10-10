"""Общие пути и функции для скриптов раздела «Животный мир»."""

import json
from pathlib import Path

WORK_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = WORK_DIR.parent.parent.parent
DATA_DIR = WORK_DIR / "data"
SPARQL_DIR = WORK_DIR / "sparql"
GENERATED_DIR = DATA_DIR / "generated"

ONTOLOGY_PATH = DATA_DIR / "ontology.json"
FACTS_PATH = DATA_DIR / "wikidata_facts.json"
GRAPH_PATH = DATA_DIR / "wikidata_graph.json"
CONCEPTS_PATH = WORK_DIR / "concepts.json"


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_ontology() -> dict:
    return load_json(ONTOLOGY_PATH)


def web_dir(ontology: dict) -> Path:
    return REPO_ROOT / ontology["web_dir"]


def article_path(ontology: dict, slug: str) -> Path:
    return web_dir(ontology) / "articles" / f"{slug}.md"


def concepts_by_slug(ontology: dict) -> dict:
    return {c["slug"]: c for c in ontology["concepts"]}


def related(ontology: dict, slug: str) -> list[tuple[str, str, str]]:
    """Все связи понятия: (откуда, тип, куда), в которых оно участвует."""
    return [tuple(r) for r in ontology["relations"] if slug in (r[0], r[2])]
