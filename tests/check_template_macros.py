# -*- coding: utf-8 -*-
"""模板宏自检（修正版）：未定义宏 / 死宏 / csrf_field 单一来源。

要点：
  - Jinja 里子模板能调用 {% extends %} 的那个模板中定义的宏，所以 base.html 的宏对子模板可用；
  - {% import ... with context %} 也要认；
  - base.html 的 ai_* 宏是给子模板用的"宏库"，不算死宏。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TPL = ROOT / "templates"

BUILTINS = {
    "range", "dict", "lipsum", "cycler", "joiner", "namespace", "super", "loop", "self",
    "url_for", "get_flashed_messages", "can", "has_role", "status_text", "urgency_text",
    "relation_text", "house_status_text", "source_text", "order_status_text", "order_urgency_text",
    "csrf_token", "len", "str", "int", "float", "list", "set", "tuple", "trans",
}
#: 允许"定义给未来使用"的宏白名单（base.html 是共享宏库）
LIBRARY_OK = {"ai_page_url", "ai_new_url", "ai_chat_url", "ai_messages_url", "ai_actions_url",
              "ai_action_url", "ai_action_confirm_url", "ai_action_cancel_url", "csrf_field"}

DEFINE = re.compile(r"\{%-?\s*macro\s+(\w+)\s*\(")
CALL = re.compile(r"\{\{-?\s*(\w+)\s*\(")
EXTENDS = re.compile(r"\{%-?\s*extends\s+'([^']+)'")
IMPORT = re.compile(r"\{%-?\s*from\s+'([^']+)'\s+import\s+(.+?)\s*(?:with\s+context\s*)?-?%\}")

#: 内置全局函数（app.py 注入的"函数式"变量），不是宏
GLOBALS = {"can", "status_text", "urgency_text", "relation_text", "house_status_text",
           "order_status_text", "order_urgency_text", "source_text", "has_role"}


def main() -> int:
    sources = {p.name: p.read_text(encoding="utf-8") for p in sorted(TPL.glob("*.html"))}
    defined = {name: set(DEFINE.findall(text)) for name, text in sources.items()}

    def available_for(name: str) -> set[str]:
        out = set(defined.get(name, set()))
        # 沿 extends 链向上收集（Jinja：父模板的宏对子模板可见）
        seen, current = set(), name
        while True:
            match = EXTENDS.search(sources.get(current, ""))
            if not match:
                break
            parent = match.group(1)
            if parent in seen:
                break
            seen.add(parent)
            out |= set(defined.get(parent, set()))
            current = parent
        # import 进来的宏
        for target, names in IMPORT.findall(sources.get(name, "")):
            for raw in names.split(","):
                imported = raw.strip().split(" as ")[-1].strip()
                if imported:
                    out.add(imported)
        return out

    problems: list[str] = []
    for name, text in sources.items():
        avail = available_for(name)
        for called in CALL.findall(text):
            if called in BUILTINS or called in GLOBALS or called in avail:
                continue
            problems.append(f"{name}: 调用了未定义的宏 {called}()")

    all_calls: set[str] = set()
    for text in sources.values():
        all_calls.update(CALL.findall(text))
    for name, macros in defined.items():
        if name.startswith("_"):
            continue
        for macro in macros:
            if macro in all_calls or macro in LIBRARY_OK:
                continue
            problems.append(f"{name}: 定义了宏 {macro}() 但全项目没人调用（死宏？）")

    if "csrf_field" not in defined.get("_macros.html", set()):
        problems.append("_macros.html 里没有 csrf_field() 宏（单一来源被破坏）")
    if "{% macro csrf_field" in sources.get("base.html", ""):
        problems.append("base.html 又定义了本地 csrf_field() 宏（会与 _macros.html 冲突）")

    call_sites = sum(text.count("csrf_field()") for name, text in sources.items() if name != "_macros.html")
    if call_sites < 5:
        problems.append(f"csrf_field() 调用点过少（{call_sites} 处），可能被误删")

    print(f"模板宏自检：{len(sources)} 个文件，问题 {len(problems)} 个")
    for item in problems:
        print("  - " + item)
    if problems:
        return 1
    print("宏定义/调用一致（无未定义宏、无死宏、csrf_field 单一来源且在用）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
