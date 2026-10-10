#!/usr/bin/env python3
"""Генерация текстов статей с помощью Claude API.

Для каждого понятия из data/ontology.json модель получает:
  - название и краткое описание понятия;
  - факты из WikiData (data/wikidata_facts.json);
  - связанные понятия раздела из онтологии;
и пишет объяснение «для десятилетнего ребёнка». Результат (только тело
статьи, без заголовка и служебных блоков) сохраняется в data/generated/<slug>.md.
Финальные статьи собирает build_articles.py.

Ключ API: переменная окружения ANTHROPIC_API_KEY или файл .env рядом с README.

Запуск:
  python scripts/generate_articles.py              # только отсутствующие статьи
  python scripts/generate_articles.py --force      # перегенерировать все
  python scripts/generate_articles.py mammals fish # только выбранные
"""

import argparse
import sys

import anthropic
from dotenv import load_dotenv

from common import FACTS_PATH, GENERATED_DIR, WORK_DIR, concepts_by_slug, load_json, load_ontology, related

MODEL = "claude-opus-5-5"

SYSTEM_PROMPT = """Ты автор детской энциклопедии KidBook. Пишешь на русском языке для детей примерно десяти лет.

Правила:
- Объясняй просто, как объяснил бы десятилетнему ребёнку: короткие предложения, понятные слова, сравнения с тем, что ребёнок видит дома, во дворе, в школе.
- Каждый новый термин сразу объясняй простыми словами.
- Пиши только достоверные факты. Опирайся на сведения из WikiData, которые даны в запросе. Если не уверен в цифре, не называй её.
- Обращайся к читателю на «ты», тон дружелюбный, без сюсюканья.
- Никаких эмодзи и смайликов.
- Формат: GitHub Flavored Markdown. Не пиши заголовок первого уровня (он будет добавлен автоматически). Используй разделы второго уровня (##).
- Объём: 350-550 слов."""

ARTICLE_TEMPLATE = """Объясни для десятилетнего ребёнка понятие «{name}».

О чём должна быть статья: {description}

Сведения из WikiData:
{facts}

Связанные понятия нашего раздела (упомяни их в тексте естественно, своими словами, хотя бы по одному разу):
{related}

Структура статьи:
1. Вступление из 2-3 предложений без заголовка: что это такое, одной простой мыслью.
2. Два-четыре раздела ## с основным рассказом. Где уместно, используй примеры животных из WikiData.
3. Раздел «## Интересные факты»: нумерованный список из 3-5 удивительных, но правдивых фактов.
4. Раздел «## Проверь себя»: 3 коротких вопроса к тексту (без ответов).

Верни только текст статьи в Markdown, без пояснений до и после."""


def format_facts(f: dict, ontology_concept: dict) -> str:
    lines = [f"- WikiData: {f['wikidata_id']} ({f.get('label')}; англ. {f.get('label_en')})"]
    if f.get("description"):
        lines.append(f"- Описание: {f['description']}")
    if f.get("taxon_name"):
        lines.append(f"- Научное название: {f['taxon_name']}")
    if f.get("aliases"):
        lines.append(f"- Другие названия: {', '.join(f['aliases'][:8])}")
    parents = [p["label"] for p in f.get("parents", []) if p["property"] in ("P171", "P279")]
    if parents:
        lines.append(f"- Входит в: {', '.join(parents)}")
    if f.get("subtaxa"):
        lines.append(f"- Крупные подгруппы (отряды/типы): {', '.join(str(t['label']) for t in f['subtaxa'])}")
    for ex in f.get("examples", []):
        status = f", охранный статус: {ex['status']}" if ex.get("status") else ""
        lines.append(f"- Пример: {ex['label']} ({ex['taxon_name']}) - {ex.get('description') or ''}{status}")
    for ex in f.get("examples_iucn", []):
        lines.append(f"- По данным МСОП: {ex['label']} - {ex['status']}")
    return "\n".join(lines)


def format_related(ontology: dict, slug: str) -> str:
    names = concepts_by_slug(ontology)
    labels = ontology["relation_types"]
    out = []
    for src, rel, dst in related(ontology, slug):
        out.append(f"- {names[src]['name']} -> {labels[rel]} -> {names[dst]['name']}")
    return "\n".join(out) or "- (нет)"


def generate(client: anthropic.Anthropic, prompt: str) -> str:
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        output_config={"effort": "medium"},
        # Если классификатор безопасности откажет, запрос автоматически
        # повторится на запасной модели.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"модель отказалась отвечать: {response.stop_details}")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("ответ обрезан по max_tokens")
    return "".join(b.text for b in response.content if b.type == "text").strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("slugs", nargs="*", help="какие статьи генерировать (по умолчанию все)")
    parser.add_argument("--force", action="store_true", help="перезаписать уже сгенерированные")
    parser.add_argument("--print-prompt", action="store_true", help="только показать промпт, без запроса к API")
    args = parser.parse_args()

    load_dotenv(WORK_DIR / ".env")
    ontology = load_ontology()
    facts = load_json(FACTS_PATH)
    concepts = concepts_by_slug(ontology)
    slugs = args.slugs or list(concepts)

    client = None if args.print_prompt else anthropic.Anthropic()
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)

    failed = []
    for slug in slugs:
        concept = concepts[slug]
        target = GENERATED_DIR / f"{slug}.md"
        if target.exists() and not args.force and not args.print_prompt:
            print(f"[SKIP] {slug} (уже есть, --force для перегенерации)")
            continue
        prompt = ARTICLE_TEMPLATE.format(
            name=concept["name"],
            description=concept["description"],
            facts=format_facts(facts[slug], concept),
            related=format_related(ontology, slug),
        )
        if args.print_prompt:
            print(f"===== {slug}\n{prompt}\n")
            continue
        print(f"[GEN] {slug} ...", flush=True)
        try:
            text = generate(client, prompt)
        except anthropic.AuthenticationError:
            sys.exit("Неверный ANTHROPIC_API_KEY. Проверь файл .env.")
        except TypeError as exc:  # SDK не нашёл ни ключа, ни профиля `ant auth login`
            if "authentication" not in str(exc):
                raise
            sys.exit(f"Не найден ключ Claude API. Скопируй .env.example в .env и вставь ключ:\n  {WORK_DIR / '.env'}")
        except (anthropic.APIStatusError, anthropic.APIConnectionError, RuntimeError) as exc:
            print(f"[FAIL] {slug}: {exc}", file=sys.stderr)
            failed.append(slug)
            continue
        target.write_text(text + "\n", encoding="utf-8")
        print(f"[OK] {slug}: {len(text.split())} слов")

    if failed:
        sys.exit(f"Не удалось сгенерировать: {', '.join(failed)}")


if __name__ == "__main__":
    main()
