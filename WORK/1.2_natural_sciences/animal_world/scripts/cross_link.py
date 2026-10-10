#!/usr/bin/env python3
"""Перекрёстные ссылки с учётом падежей и чисел.

Обычный поиск по строке не находит «хищников», «пищевой цепи» или
«земноводными», если в списке ключевых слов записано «хищник», «пищевая цепь»
и «земноводные». Поэтому и ключевые слова (lemmas), и текст статьи
приводятся к начальной форме морфологическим анализатором pymorphy3, и
сравниваются уже начальные формы. Многословные выражения сравниваются
пословно: «пищевой цепи» -> [пищевой, цепь] совпадает с «пищевая цепь».

Слово в тексте может разбираться неоднозначно («животных» - это и
существительное «животное», и прилагательное «животный»), поэтому для
каждого слова берутся все варианты начальной формы.

Ссылки ставятся:
  - на понятия своего раздела (ключевые слова из data/ontology.json);
  - на понятия других разделов (все concepts.json в репозитории).
Каждое понятие получает ссылку только при первом упоминании в статье.
Ссылки ставятся только между метками <!-- body:start --> и <!-- body:end -->,
то есть не трогают заголовки, диаграммы, подписи и блок «Смотри также».

Запуск:
  python scripts/cross_link.py            # проставить ссылки
  python scripts/cross_link.py --dry-run  # только показать, что будет сделано
"""

import argparse
import json
import os
import re
import sys
import urllib.parse
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pymorphy3

from common import REPO_ROOT, article_path, load_ontology

BODY_RE = re.compile(r"(<!-- body:start -->)(.*?)(<!-- body:end -->)", re.DOTALL)
# Куски, внутри которых ссылки ставить нельзя.
PROTECTED_RE = re.compile(
    r"```.*?```"              # блоки кода и диаграммы
    r"|`[^`\n]+`"             # код в строке
    r"|!\[[^\]]*\]\([^)]*\)"  # картинки
    r"|\[[^\]]*\]\([^)]*\)"   # уже существующие ссылки
    r"|<[^>]+>"               # HTML-теги и комментарии
    r"|^#{1,6} [^\n]*$",      # заголовки
    re.DOTALL | re.MULTILINE,
)
WORD_RE = re.compile(r"[А-Яа-яЁёA-Za-z]+(?:-[А-Яа-яЁёA-Za-z]+)*")

MAX_EXTERNAL_LINKS = 4
# Разделы, на которые разумно ссылаться из статей о животных. Без этого фильтра
# ссылки уходили, например, из «морских звёзд» в астрономию и из «города» в статьи об искусстве.
EXTERNAL_SECTIONS = (
    "WEB/1.1_structure_of_the_world/",
    "WEB/1.1_ustroystvo_mira/",
    "WEB/1.2_natural_sciences/neurobiology_for_teens/",
    "WEB/1.2_natural_sciences/why_science_help_understand_world/",
    "WEB/3.1_healthy lifestyle/",
    "WEB/3.2_safety_and_first_aid/",
)
# Ключевые слова других групп, которые слишком общие для ссылки из статьи о животных.
EXTERNAL_STOPLIST = {
    "биология", "природа", "мир", "жизнь", "энергия", "сон", "питание", "еда", "вода",
    "здоровье", "организм", "тело", "дети", "подростки", "человек", "люди", "наука",
    "земля", "планета", "время", "движение", "система", "игра", "игры", "охрана",
    "развитие", "рост", "среда", "защита", "цепь", "сеть", "звено", "клетка",
    "ученый", "семья", "число", "умение", "форма", "опора", "повторение", "внимание",
    "курица", "дыхание", "место", "часть", "группа", "вид", "работа", "сила", "ребенок",
    "звезда", "орган", "температура", "солнце", "дождь", "кислород", "спина", "жидкость",
    "родственник", "забота", "настроение", "окружение", "город", "условие", "причина",
    "живой", "равновесие", "скорость",
}

morph = pymorphy3.MorphAnalyzer()


# Ссылаться можно только с существительных и прилагательных (в том числе причастий),
# иначе «внимательно» превращается в ссылку на «внимание», а «повторяют» - на «повторение».
LINKABLE_POS = {"NOUN", "ADJF", "PRTF"}


@lru_cache(maxsize=None)
def normal_forms(word: str) -> frozenset[str]:
    word = word.lower().replace("ё", "е")
    return frozenset(p.normal_form.replace("ё", "е") for p in morph.parse(word) if p.tag.POS in LINKABLE_POS)


def lemma_key(phrase: str) -> tuple[str, ...]:
    """Ключевое слово -> последовательность начальных форм (по первому разбору)."""
    words = WORD_RE.findall(phrase)
    return tuple(morph.parse(w.lower())[0].normal_form.replace("ё", "е") for w in words)


@dataclass
class Target:
    key: tuple[str, ...]
    file: Path
    label: str
    external: bool


def load_internal_targets(ontology: dict) -> list[Target]:
    targets = []
    for c in ontology["concepts"]:
        for phrase in {c["name"], *c["lemmas"]}:
            targets.append(Target(lemma_key(phrase), article_path(ontology, c["slug"]), c["name"], False))
    return targets


def load_external_targets(ontology: dict) -> list[Target]:
    own_dir = (REPO_ROOT / ontology["web_dir"]).resolve()
    own_work = Path(__file__).resolve().parent.parent
    targets = []
    for path in sorted(REPO_ROOT.rglob("concepts.json")):
        if "TUTORIAL" in path.parts or path.resolve().is_relative_to(own_work):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            print(f"[WARN] {path.relative_to(REPO_ROOT)}: {exc}", file=sys.stderr)
            continue
        for section in data if isinstance(data, list) else [data]:
            for concept in section.get("concepts", []):
                raw = (concept.get("file") or "").strip()
                file = next((p for p in (REPO_ROOT / raw, REPO_ROOT / "WEB" / raw) if raw and p.is_file()), None)
                if file is None or file.resolve().is_relative_to(own_dir):
                    continue
                if not file.relative_to(REPO_ROOT).as_posix().startswith(EXTERNAL_SECTIONS):
                    continue
                for phrase in concept.get("lemmas", []):
                    if not isinstance(phrase, str):
                        continue
                    key = lemma_key(phrase)
                    if not key or (len(key) == 1 and (key[0] in EXTERNAL_STOPLIST or len(key[0]) < 5)):
                        continue
                    targets.append(Target(key, file, concept.get("name", raw), True))
    return targets


def build_index(internal: list[Target], external: list[Target]) -> dict[str, list[Target]]:
    """Индекс по первому слову ключа. Свои понятия важнее чужих, длинные ключи важнее коротких."""
    own_keys = {t.key for t in internal}
    index: dict[str, list[Target]] = {}
    for t in internal + [t for t in external if t.key not in own_keys]:
        if t.key:
            index.setdefault(t.key[0], []).append(t)
    for lst in index.values():
        lst.sort(key=lambda t: (-len(t.key), t.external))
    return index


def rel_link(from_file: Path, to_file: Path) -> str:
    return urllib.parse.quote(os.path.relpath(to_file, from_file.parent).replace("\\", "/"))


def linkify_text(text: str, index: dict, source: Path, used: set, stats: dict) -> str:
    words = list(WORD_RE.finditer(text))
    out, pos, i = [], 0, 0
    while i < len(words):
        match = None
        for first in normal_forms(words[i].group()):
            for t in index.get(first, []):
                n = len(t.key)
                if t.file.resolve() in used | {source.resolve()} or i + n > len(words):
                    continue
                if t.external and stats["external"] + stats.get("external_before", 0) >= MAX_EXTERNAL_LINKS:
                    continue
                span = words[i : i + n]
                # слова выражения должны идти подряд, разделённые только пробелами
                if any(text[a.end() : b.start()].strip() for a, b in zip(span, span[1:])):
                    continue
                if all(t.key[k] in normal_forms(span[k].group()) for k in range(n)):
                    if match is None or n > len(match.key):
                        match = t
        if match is None:
            i += 1
            continue
        n = len(match.key)
        start, end = words[i].start(), words[i + n - 1].end()
        out.append(text[pos:start])
        out.append(f"[{text[start:end]}]({rel_link(source, match.file)})")
        pos = end
        used.add(match.file.resolve())
        stats["external" if match.external else "internal"] += 1
        stats["log"].append(f"{text[start:end]!r} -> {match.label}" + (f" [{match.file.relative_to(REPO_ROOT).as_posix()}]" if match.external else ""))
        i += n
    out.append(text[pos:])
    return "".join(out)


def linkify_body(body: str, index: dict, source: Path, stats: dict) -> str:
    # при повторном запуске не ставим вторую ссылку на уже связанные понятия
    used: set = {
        (source.parent / urllib.parse.unquote(href)).resolve()
        for href in re.findall(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)", body)
    }
    # уже стоящие ссылки на другие разделы тоже считаются в лимит MAX_EXTERNAL_LINKS
    section_dir = source.parent.parent.resolve()
    stats["external_before"] = sum(not p.is_relative_to(section_dir) for p in used)
    result, last = [], 0
    for m in PROTECTED_RE.finditer(body):
        result.append(linkify_text(body[last : m.start()], index, source, used, stats))
        result.append(m.group())
        last = m.end()
    result.append(linkify_text(body[last:], index, source, used, stats))
    return "".join(result)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="ничего не записывать")
    parser.add_argument("--no-external", action="store_true", help="только ссылки внутри раздела")
    args = parser.parse_args()

    ontology = load_ontology()
    internal = load_internal_targets(ontology)
    external = [] if args.no_external else load_external_targets(ontology)
    index = build_index(internal, external)
    print(f"[INFO] ключей своего раздела: {len(internal)}, других разделов: {len(external)}")

    total_int = total_ext = 0
    for c in ontology["concepts"]:
        path = article_path(ontology, c["slug"])
        if not path.exists():
            print(f"[WARN] нет статьи {path.relative_to(REPO_ROOT)}", file=sys.stderr)
            continue
        text = path.read_text(encoding="utf-8")
        m = BODY_RE.search(text)
        if not m:
            print(f"[WARN] {path.name}: нет меток body:start/body:end", file=sys.stderr)
            continue
        stats = {"internal": 0, "external": 0, "log": []}
        body = linkify_body(m.group(2), index, path, stats)
        new_text = text[: m.start(2)] + body + text[m.end(2) :]
        total_int += stats["internal"]
        total_ext += stats["external"]
        print(f"{'[DRY]' if args.dry_run else '[MOD]'} {path.name}: +{stats['internal']} своих, +{stats['external']} внешних")
        for line in stats["log"]:
            print(f"      {line}")
        if not args.dry_run and new_text != text:
            path.write_text(new_text, encoding="utf-8")

    print(f"[DONE] ссылок внутри раздела: {total_int}, на другие разделы: {total_ext}")


if __name__ == "__main__":
    main()
