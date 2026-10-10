#!/usr/bin/env python3
"""Извлечение знаний о животных из WikiData.

Выполняет SPARQL-запросы из папки sparql/ и сохраняет:
  data/wikidata_facts.json  - факты по каждому понятию (описания, синонимы,
                              родительские понятия, отряды, животные-примеры)
  data/wikidata_graph.json  - подграф связей между понятиями раздела
  WEB/.../images/*.jpg      - иллюстрации из Wikimedia Commons (свойство P18)

Запуск: python scripts/fetch_wikidata.py [--no-images]
"""

import argparse
import sys
import time
import urllib.parse

import requests

from common import DATA_DIR, FACTS_PATH, GRAPH_PATH, SPARQL_DIR, load_json, load_ontology, save_json, web_dir

IMAGE_SOURCES_PATH = DATA_DIR / "image_sources.json"

ENDPOINT = "https://query.wikidata.org/sparql"
HEADERS = {"User-Agent": "KidBookLab/1.0 (educational project; github.com/shwars/2026_kidbook)"}

# Откуда брать отряды для понятий-таксонов. У пресмыкающихся WikiData
# использует кладистическую систему, и отряды не выводятся к классу Reptilia
# цепочкой P171, поэтому их QID перечислены явно.
ORDER_ROOTS = {
    "mammals": ["Q7377"],
    "birds": ["Q5113"],
    "amphibians": ["Q10908"],
    "insects": ["Q1390"],
    "arachnids": ["Q1358"],
    "fish": ["Q127282", "Q25371"],
}
EXPLICIT_SUBTAXA = {
    "reptiles": ["Q122422", "Q223044", "Q25363", "Q15099738"],
    "invertebrates": ["Q1360", "Q25326", "Q25441", "Q25522", "Q44631"],
}

# Животные-примеры для каждого понятия (по латинскому названию, свойство P225).
EXAMPLES = {
    "animals": ["Canis lupus", "Aquila chrysaetos", "Apis mellifera", "Octopus vulgaris"],
    "vertebrates": ["Felis catus", "Ciconia ciconia", "Lacerta agilis", "Rana temporaria", "Esox lucius"],
    "invertebrates": ["Lumbricus terrestris", "Octopus vulgaris", "Aurelia aurita", "Helix pomatia"],
    "mammals": ["Balaenoptera musculus", "Ornithorhynchus anatinus", "Ursus arctos", "Erinaceus europaeus"],
    "birds": ["Ciconia ciconia", "Grus grus", "Corvus cornix", "Aptenodytes forsteri"],
    "reptiles": ["Lacerta agilis", "Vipera berus", "Crocodylus niloticus", "Chelonia mydas"],
    "amphibians": ["Rana temporaria", "Pelophylax ridibundus", "Lissotriton vulgaris", "Ambystoma mexicanum"],
    "fish": ["Carcharodon carcharias", "Salmo salar", "Esox lucius", "Carassius gibelio"],
    "insects": ["Apis mellifera", "Coccinella septempunctata", "Pieris brassicae", "Danaus plexippus"],
    "arachnids": ["Araneus diadematus", "Ixodes ricinus", "Latrodectus mactans"],
    "predators": ["Canis lupus", "Panthera leo", "Panthera tigris", "Vulpes vulpes"],
    "herbivores": ["Alces alces", "Giraffa camelopardalis", "Lepus timidus", "Capreolus capreolus"],
    "omnivores": ["Ursus arctos", "Sus scrofa", "Corvus cornix", "Erinaceus europaeus"],
    "food_chain": ["Lepus timidus", "Vulpes vulpes", "Aquila chrysaetos"],
    "habitat": ["Ursus maritimus", "Camelus dromedarius", "Megaptera novaeangliae"],
    "migration": ["Megaptera novaeangliae", "Danaus plexippus", "Salmo salar", "Grus grus"],
    "hibernation": ["Erinaceus europaeus", "Marmota monax", "Ursus arctos", "Rana temporaria"],
    "metamorphosis": ["Danaus plexippus", "Rana temporaria", "Pieris brassicae"],
    "camouflage": ["Chamaeleo calyptratus", "Lepus timidus", "Mantis religiosa", "Phasmatodea"],
    "endangered_species": ["Panthera tigris altaica", "Ailuropoda melanoleuca", "Ursus maritimus"],
}

# Если у понятия в WikiData нет картинки (P18), берём картинку этого животного.
IMAGE_FALLBACK = {
    "animals": "Canis lupus",
    "mammals": "Erinaceus europaeus",
    "predators": "Panthera leo",
    "herbivores": "Giraffa camelopardalis",
    "omnivores": "Ursus arctos",
    "food_chain": "Vulpes vulpes",
    "habitat": "Ursus maritimus",
    "migration": "Grus grus",
    "hibernation": "Erinaceus europaeus",
    "metamorphosis": "Danaus plexippus",
    "camouflage": "Chamaeleo calyptratus",
    "endangered_species": "Panthera tigris altaica",
}


# Картинка понятия в WikiData не подходит для детской статьи о животных:
# у «плотоядных» (Q81875) это росянка - хищное растение.
IMAGE_OVERRIDE = {
    "predators": "Panthera leo",
}


def read_query(name: str, **params: str) -> str:
    text = (SPARQL_DIR / name).read_text(encoding="utf-8")
    for key, value in params.items():
        text = text.replace(f"%%{key}%%", value)
    return text


def run_sparql(query: str, retries: int = 4) -> dict:
    for attempt in range(retries):
        try:
            resp = requests.get(
                ENDPOINT, params={"query": query, "format": "json"}, headers=HEADERS, timeout=90
            )
        except requests.RequestException as exc:
            print(f"  [WARN] сетевая ошибка: {exc}", file=sys.stderr)
        else:
            if resp.ok:
                time.sleep(1)  # бережём публичный сервис
                return resp.json()
            print(f"  [WARN] HTTP {resp.status_code}", file=sys.stderr)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError("SPARQL-запрос не выполнился после нескольких попыток")


def rows(result: dict) -> list[dict]:
    return [{k: v["value"] for k, v in b.items()} for b in result["results"]["bindings"]]


def qid(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def commons_file(image_uri: str) -> str:
    return urllib.parse.unquote(image_uri.rsplit("/", 1)[-1])


def fetch_concepts(concepts: list[dict]) -> dict:
    items = " ".join(f"wd:{c['wikidata_id']}" for c in concepts)
    facts = {}
    by_qid = {c["wikidata_id"]: c["slug"] for c in concepts}

    for row in rows(run_sparql(read_query("01_concept_facts.rq", ITEMS=items))):
        slug = by_qid[qid(row["item"])]
        if slug in facts:  # несколько картинок у одной сущности: берём первую
            continue
        facts[slug] = {
            "wikidata_id": qid(row["item"]),
            "label": row.get("label"),
            "description": row.get("description"),
            "label_en": row.get("enLabel"),
            "taxon_name": row.get("taxonName"),
            "aliases": sorted(a for a in row.get("aliases", "").split("|") if a),
            "image": commons_file(row["image"]) if "image" in row else None,
            "ruwiki": row.get("article"),
            "parents": [],
            "subtaxa": [],
            "examples": [],
        }

    for row in rows(run_sparql(read_query("02_parents.rq", ITEMS=items))):
        slug = by_qid[qid(row["item"])]
        facts[slug]["parents"].append(
            {"property": qid(row["prop"]), "id": qid(row["parent"]), "label": row["parentLabel"]}
        )
    return facts


def fetch_subtaxa(facts: dict) -> None:
    for slug, roots in ORDER_ROOTS.items():
        found = []
        for root in roots:
            res = run_sparql(read_query("03_orders_under_taxon.rq", ROOT=root, LIMIT="8"))
            found += [
                {"id": qid(r["order"]), "label": r["label"], "sitelinks": int(r["sitelinks"])}
                for r in rows(res)
            ]
        unique = {t["id"]: t for t in found}
        facts[slug]["subtaxa"] = sorted(unique.values(), key=lambda t: -t["sitelinks"])[:10]
        print(f"  {slug}: {', '.join(t['label'] for t in facts[slug]['subtaxa'])}")

    for slug, ids in EXPLICIT_SUBTAXA.items():
        res = run_sparql(read_query("01_concept_facts.rq", ITEMS=" ".join(f"wd:{i}" for i in ids)))
        # одна сущность может прийти несколькими строками (несколько картинок или статей)
        unique = {qid(r["item"]): r for r in rows(res)}
        facts[slug]["subtaxa"] = [
            {"id": i, "label": r.get("label"), "description": r.get("description")}
            for i, r in unique.items()
        ]
        print(f"  {slug}: {', '.join(str(t['label']) for t in facts[slug]['subtaxa'])}")


def fetch_examples(facts: dict) -> dict:
    names = sorted(
        {n for lst in EXAMPLES.values() for n in lst} | set(IMAGE_FALLBACK.values()) | set(IMAGE_OVERRIDE.values())
    )
    values = " ".join(f'"{n}"' for n in names)
    by_name: dict[str, dict] = {}
    for r in rows(run_sparql(read_query("04_taxa_by_latin_name.rq", NAMES=values))):
        name = r["taxonName"]
        if name in by_name:  # берём самый известный объект с таким названием
            continue
        by_name[name] = {
            "taxon_name": name,
            "id": qid(r["item"]),
            "label": r.get("label"),
            "description": r.get("description"),
            "status": r.get("statusLabel"),
            "image": commons_file(r["image"]) if "image" in r else None,
        }
    missing = [n for n in names if n not in by_name]
    if missing:
        print(f"  [WARN] не найдены в WikiData: {', '.join(missing)}", file=sys.stderr)
    for slug, lst in EXAMPLES.items():
        facts[slug]["examples"] = [by_name[n] for n in lst if n in by_name]
    return by_name


def fetch_endangered(limit: int = 12) -> list[dict]:
    result = []
    for r in rows(run_sparql(read_query("05_endangered_species.rq"))):
        if len(result) >= limit:
            break
        item = qid(r["item"])
        if not run_sparql(read_query("06_is_animal.rq", ITEM=item)).get("boolean"):
            continue
        result.append({"id": item, "label": r["label"], "status": r["statusLabel"]})
    print(f"  вымирающие: {', '.join(e['label'] for e in result)}")
    return result


def fetch_graph(concepts: list[dict]) -> list[list[str]]:
    by_qid = {c["wikidata_id"]: c["slug"] for c in concepts}
    items = " ".join(f"wd:{q}" for q in by_qid)
    try:
        res = run_sparql(read_query("07_concept_graph.rq", ITEMS=items))
    except RuntimeError as exc:
        print(f"  [WARN] граф не получен: {exc}", file=sys.stderr)
        return []
    return sorted([by_qid[qid(r["a"])], by_qid[qid(r["b"])]] for r in rows(res))


def download_images(facts: dict, by_name: dict, images_dir, refresh: bool) -> None:
    images_dir.mkdir(parents=True, exist_ok=True)
    # какой файл Commons лежит под каким именем, чтобы не качать заново и не перепутать
    downloaded = load_json(IMAGE_SOURCES_PATH) if IMAGE_SOURCES_PATH.exists() else {}
    for slug, f in facts.items():
        source = None if slug in IMAGE_OVERRIDE else f["image"]
        if not source and (slug in IMAGE_OVERRIDE or slug in IMAGE_FALLBACK):
            fallback = by_name.get(IMAGE_OVERRIDE.get(slug) or IMAGE_FALLBACK[slug])
            source = fallback and fallback["image"]
            f["image_from"] = fallback and fallback["label"]
        if not source:
            print(f"  [WARN] нет картинки для {slug}", file=sys.stderr)
            continue
        f["image_source"] = "https://commons.wikimedia.org/wiki/File:" + urllib.parse.quote(source.replace(" ", "_"))
        existing = sorted(images_dir.glob(f"{slug}.*"))
        if existing and downloaded.get(slug) == source and not refresh:
            f["image_file"] = f"images/{existing[0].name}"
            print(f"  {slug}: уже скачано ({existing[0].name})")
            continue
        for old in existing:
            old.unlink()
        url = "https://commons.wikimedia.org/wiki/Special:FilePath/" + urllib.parse.quote(source) + "?width=800"
        resp = None
        for attempt in range(4):
            resp = requests.get(url, headers=HEADERS, timeout=60)
            if resp.status_code != 429:
                break
            time.sleep(10 * (attempt + 1))
        if not resp.ok:
            print(f"  [WARN] {slug}: HTTP {resp.status_code} при загрузке {source}", file=sys.stderr)
            continue
        ext = ".png" if "png" in resp.headers.get("Content-Type", "") else ".jpg"
        target = images_dir / f"{slug}{ext}"
        target.write_bytes(resp.content)
        f["image_file"] = f"images/{target.name}"
        downloaded[slug] = source
        save_json(IMAGE_SOURCES_PATH, downloaded)
        print(f"  {slug}: {source} -> {target.name} ({len(resp.content) // 1024} КБ)")
        time.sleep(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-images", action="store_true", help="не скачивать картинки")
    parser.add_argument("--refresh-images", action="store_true", help="перекачать уже скачанные картинки")
    args = parser.parse_args()

    ontology = load_ontology()
    concepts = ontology["concepts"]

    print("[1/5] Сведения о понятиях")
    facts = fetch_concepts(concepts)
    print("[2/5] Отряды и подгруппы")
    fetch_subtaxa(facts)
    print("[3/5] Животные-примеры")
    by_name = fetch_examples(facts)
    print("[4/5] Вымирающие виды и подграф")
    endangered = fetch_endangered()
    facts["endangered_species"]["examples_iucn"] = endangered
    graph = fetch_graph(concepts)
    print(f"  связей в подграфе WikiData: {len(graph)}")

    if not args.no_images:
        print("[5/5] Картинки из Wikimedia Commons")
        download_images(facts, by_name, web_dir(ontology) / "images", args.refresh_images)

    save_json(FACTS_PATH, facts)
    save_json(GRAPH_PATH, {"source": "WikiData, свойства P171 и P279", "edges": graph})
    print(f"[DONE] {FACTS_PATH.name}, {GRAPH_PATH.name}")


if __name__ == "__main__":
    main()
