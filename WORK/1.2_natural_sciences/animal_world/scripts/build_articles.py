#!/usr/bin/env python3
"""Сборка раздела из сгенерированных текстов, фактов WikiData и онтологии.

Создаёт:
  WEB/.../articles/<slug>.md  - статьи: заголовок, картинка, текст модели,
                                диаграмма связей понятия, «Смотри также»,
                                источники, авторы
  WEB/.../index.md            - страница раздела с картой понятий
  WEB/.../glossary.md         - словарь терминов раздела
  WORK/.../concepts.json      - список понятий в формате репозитория

После сборки нужно запустить cross_link.py, чтобы проставить ссылки в тексте.

Запуск: python scripts/build_articles.py
"""

import re
import sys

from common import (
    CONCEPTS_PATH,
    FACTS_PATH,
    GENERATED_DIR,
    GRAPH_PATH,
    REPO_ROOT,
    article_path,
    concepts_by_slug,
    load_json,
    load_ontology,
    related,
    save_json,
    web_dir,
)

MODELS_LINE = "*Использованные нейросети: Claude (Anthropic), модель claude-opus-5-5 - генерация текстов статей.*"
CYRILLIC_RE = re.compile(r"^[А-Яа-яЁё][А-Яа-яЁё \-]*$")


def authors_line(ontology: dict) -> str:
    return f"Авторы: {', '.join(ontology['members'])}  "


def mermaid_id(slug: str) -> str:
    return slug.replace("_", "")


def neighbourhood_diagram(ontology: dict, slug: str) -> str:
    names = concepts_by_slug(ontology)
    labels = ontology["relation_types"]
    edges = related(ontology, slug)
    nodes = sorted({s for e in edges for s in (e[0], e[2])} | {slug})
    lines = ["```mermaid", "graph LR"]
    for n in nodes:
        lines.append(f'    {mermaid_id(n)}["{names[n]["name"]}"]')
    for src, rel, dst in edges:
        lines.append(f"    {mermaid_id(src)} -->|{labels[rel]}| {mermaid_id(dst)}")
    lines.append("    classDef current fill:#e8f0fe,stroke:#1a56db,stroke-width:2px")
    lines.append(f"    class {mermaid_id(slug)} current")
    lines.append("```")
    return "\n".join(lines)


def see_also(ontology: dict, slug: str) -> list[str]:
    names = concepts_by_slug(ontology)
    labels = ontology["relation_types"]
    items = []
    for src, rel, dst in related(ontology, slug):
        other = dst if src == slug else src
        phrase = f"{names[src]['name']} → {labels[rel]} → {names[dst]['name']}"
        items.append(f"- [{names[other]['name']}]({other}.md): {phrase.lower()}")
    return list(dict.fromkeys(items))


def image_block(concept: dict, fact: dict) -> str:
    if not fact.get("image_file"):
        return ""
    caption = "Иллюстрация из Wikimedia Commons"
    if fact.get("image_from"):
        caption += f", на снимке: {fact['image_from']}"
    return (
        f'![{concept["name"]}](../{fact["image_file"]} "{concept["name"]}")\n\n'
        f"*{caption} ([источник]({fact['image_source']})).*\n"
    )


def sources_block(fact: dict) -> list[str]:
    lines = [f"- WikiData: [{fact['wikidata_id']}](https://www.wikidata.org/wiki/{fact['wikidata_id']})"]
    if fact.get("ruwiki"):
        lines.append(f"- Википедия: [{fact['label']}]({fact['ruwiki']})")
    return lines


def build_article(ontology: dict, concept: dict, fact: dict) -> str | None:
    raw = GENERATED_DIR / f"{concept['slug']}.md"
    if not raw.exists():
        print(f"[WARN] нет текста {raw.relative_to(REPO_ROOT)}, сначала запусти generate_articles.py", file=sys.stderr)
        return None
    body = raw.read_text(encoding="utf-8").strip()
    body = re.sub(r"^# .*\n+", "", body)  # заголовок первого уровня добавляем сами
    parts = [
        f"# {concept['name']}",
        "",
        image_block(concept, fact),
        "<!-- body:start -->",
        body,
        "<!-- body:end -->",
        "",
        "## Как это связано с другими понятиями",
        "",
        neighbourhood_diagram(ontology, concept["slug"]),
        "",
        "## Смотри также",
        "",
        *see_also(ontology, concept["slug"]),
        f"- [Все статьи раздела «Животный мир»](../index.md)",
        "",
        "## Источники",
        "",
        *sources_block(fact),
        "",
        "---",
        f"Автор: {concept['author']}  ",
        MODELS_LINE,
        "",
    ]
    return "\n".join(parts)


def concept_lemmas(concept: dict, fact: dict) -> list[str]:
    aliases = [a for a in fact.get("aliases", []) if CYRILLIC_RE.match(a) and len(a.split()) <= 3]
    seen, out = set(), []
    for lemma in [concept["name"].lower(), *concept["lemmas"], *aliases]:
        if lemma.lower() not in seen:
            seen.add(lemma.lower())
            out.append(lemma)
    return out


def build_concepts_json(ontology: dict, facts: dict) -> list:
    names = concepts_by_slug(ontology)
    base = ontology["web_dir"]
    items = []
    for c in ontology["concepts"]:
        rels = [
            {"type": rel, "label": ontology["relation_types"][rel], "target": f"{base}/articles/{dst}"}
            for src, rel, dst in ontology["relations"]
            if src == c["slug"]
        ]
        items.append(
            {
                "id": f"{base}/articles/{c['slug']}",
                "description": c["description"],
                "lemmas": concept_lemmas(c, facts[c["slug"]]),
                "name": c["name"],
                "file": f"{base}/articles/{c['slug']}.md",
                "wikidata_id": c["wikidata_id"],
                "author": c["author"],
                "relations": rels,
            }
        )
    assert len(items) == len(names)
    return [
        {
            "section": ontology["section"],
            "topic": ontology["topic"],
            "author_group": ontology["author_group"],
            "members": ontology["members"],
            "concepts": items,
        }
    ]


def full_ontology_diagram(ontology: dict) -> str:
    names = concepts_by_slug(ontology)
    labels = ontology["relation_types"]
    blocks: dict[str, list[str]] = {}
    for c in ontology["concepts"]:
        blocks.setdefault(c["block"], []).append(c["slug"])
    lines = ["```mermaid", "graph TD"]
    for i, (block, slugs) in enumerate(blocks.items()):
        lines.append(f'    subgraph b{i}["{block}"]')
        for s in slugs:
            lines.append(f'        {mermaid_id(s)}["{names[s]["name"]}"]')
        lines.append("    end")
    for src, rel, dst in ontology["relations"]:
        arrow = "-->" if rel == "subclass_of" else "-.->"
        lines.append(f"    {mermaid_id(src)} {arrow}|{labels[rel]}| {mermaid_id(dst)}")
    lines.append("```")
    return "\n".join(lines)


def build_index(ontology: dict) -> str:
    blocks: dict[str, list[dict]] = {}
    for c in ontology["concepts"]:
        blocks.setdefault(c["block"], []).append(c)
    lines = [
        "# Животный мир",
        "",
        "Раздел детской энциклопедии KidBook о том, как устроены и как живут животные. "
        "Здесь можно узнать, на какие группы учёные делят животных, кто кого ест в природе, "
        "зачем птицы улетают на юг, как гусеница становится бабочкой и почему некоторые виды "
        "нуждаются в нашей защите.",
        "",
        "Раздел относится к теме **1.2 Естественные науки в жизни**.",
        "",
        "## Карта понятий",
        "",
        "Сплошные стрелки показывают, какая группа входит в какую. Пунктирные стрелки показывают "
        "другие связи: кто кого ест, кто где живёт, кто как защищается.",
        "",
        full_ontology_diagram(ontology),
        "",
        "## Статьи",
        "",
    ]
    for block, concepts in blocks.items():
        lines.append(f"### {block}")
        lines.append("")
        for c in concepts:
            lines.append(f"- [{c['name']}](articles/{c['slug']}.md) - {c['description']}")
        lines.append("")
    lines += [
        "## Словарь",
        "",
        "Краткие определения всех понятий раздела собраны в [словаре терминов](glossary.md).",
        "",
        "---",
        authors_line(ontology),
        MODELS_LINE,
        "",
    ]
    return "\n".join(lines)


def definition(slug: str) -> str:
    """Первое предложение статьи без повтора термина: «Рыбы - это животные...» -> «животные...»."""
    raw = GENERATED_DIR / f"{slug}.md"
    if not raw.exists():
        return ""
    for paragraph in raw.read_text(encoding="utf-8").split("\n\n"):
        paragraph = paragraph.strip()
        if paragraph and not paragraph.startswith(("#", "!", "-", "|", ">", "`")):
            match = re.match(r"(.+?[.!?])(\s|$)", paragraph.replace("\n", " "))
            sentence = match.group(1) if match else paragraph
            return re.sub(r"^.*? - это ", "", sentence, count=1)
    return ""


def build_glossary(ontology: dict, facts: dict) -> str:
    lines = [
        "# Словарь терминов раздела «Животный мир»",
        "",
        "Определения взяты из первых предложений статей. В скобках - латинское название группы "
        "(если это группа животных) и ссылка на понятие в WikiData.",
        "",
    ]
    for c in sorted(ontology["concepts"], key=lambda c: c["name"]):
        f = facts[c["slug"]]
        extra = [f"WikiData: [{c['wikidata_id']}](https://www.wikidata.org/wiki/{c['wikidata_id']})"]
        if f.get("taxon_name"):
            extra.insert(0, f"лат. *{f['taxon_name']}*")
        lines.append(f"**[{c['name']}](articles/{c['slug']}.md)** - {definition(c['slug'])} ({'; '.join(extra)})")
        lines.append("")
    lines += ["---", authors_line(ontology), MODELS_LINE, ""]
    return "\n".join(lines)


def wikidata_graph_diagram(ontology: dict, edges: list) -> str:
    names = concepts_by_slug(ontology)
    nodes = sorted({s for e in edges for s in e})
    lines = ["```mermaid", "graph BT"]
    for n in nodes:
        lines.append(f'    {mermaid_id(n)}["{names[n]["name"]}"]')
    for a, b in edges:
        lines.append(f"    {mermaid_id(a)} --> {mermaid_id(b)}")
    lines.append("```")
    return "\n".join(lines)


def inject(path, marker: str, content: str) -> None:
    """Заменяет содержимое между <!-- marker:start --> и <!-- marker:end --> в отчёте."""
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(rf"(<!-- {marker}:start -->)(.*?)(<!-- {marker}:end -->)", re.DOTALL)
    if not pattern.search(text):
        print(f"[WARN] в {path.name} нет меток {marker}", file=sys.stderr)
        return
    path.write_text(pattern.sub(lambda m: f"{m.group(1)}\n{content}\n{m.group(3)}", text), encoding="utf-8")


def main() -> None:
    ontology = load_ontology()
    facts = load_json(FACTS_PATH)
    out_dir = web_dir(ontology)
    (out_dir / "articles").mkdir(parents=True, exist_ok=True)

    built = 0
    for concept in ontology["concepts"]:
        text = build_article(ontology, concept, facts[concept["slug"]])
        if text is None:
            continue
        article_path(ontology, concept["slug"]).write_text(text, encoding="utf-8")
        built += 1

    (out_dir / "index.md").write_text(build_index(ontology), encoding="utf-8")
    (out_dir / "glossary.md").write_text(build_glossary(ontology, facts), encoding="utf-8")
    save_json(CONCEPTS_PATH, build_concepts_json(ontology, facts))

    graph = load_json(GRAPH_PATH)
    readme = CONCEPTS_PATH.parent / "README.md"
    inject(readme, "ontology", full_ontology_diagram(ontology))
    inject(readme, "wikidata-graph", wikidata_graph_diagram(ontology, graph["edges"]))
    print(f"[DONE] статей: {built}/{len(ontology['concepts'])}, связей онтологии: {len(ontology['relations'])}, "
          f"связей из WikiData: {len(graph['edges'])}")
    print("Дальше: python scripts/cross_link.py")


if __name__ == "__main__":
    main()
