""" Этап концентрации знаний.
Вызывается ПОСЛЕ маршрутизации (route_query) и ДО запуска ReAct-цикла.
Принимает выбранные плагины с их полной методичкой, извлекает только ту её
часть, которая нужна для конкретного вопроса пользователя, и формирует
самодостаточный план:
    concentrated_knowledge - сжатая методичка, релевантная вопросу
    planned_tools - список инструментов в порядке вызова
    planned_widgets - выбранные виджеты с ОРИГИНАЛЬНЫМИ config-ами
    plan - пошаговый план сбора данных и ответа
Гибридный подход:
    - LLM только ВЫБИРАЕТ имена intent-ов (не копирует их config)
    - config-и подтягиваются вербатимом из SKILL.md
    - если LLM ошиблась / JSON не распарсился - fallback на полный контекст
      (без дублирования planned_widgets, чтобы не раздувать промпт) """

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from openai import OpenAI

# ОСОБЫЕ ВИДЖЕТЫ - всегда пробрасываются в planned_widgets,
# независимо от решения концентратора
SPECIAL_WIDGET_TYPES = {"action_card", "ActionCard"}

_MAX_TOKENS = 8096
_TEMPERATURE = 0.0

def is_special_widget(widget_type: str) -> bool:
    """ True, если виджет особый (action_card и др.) - требует LLM-генерации
    и всегда force-included. False - системный виджет (placeholder) """
    return widget_type in SPECIAL_WIDGET_TYPES

@dataclass
class ConcentratedContext:
    """ Результат этапа концентрации знаний """
    concentrated_knowledge: str = ""
    planned_tools: List[str] = field(default_factory=list)
    planned_widgets: List[Dict[str, Any]] = field(default_factory=list)
    plan: str = ""
    fallback_used: bool = False
    raw_llm_output: str = ""

CONCENTRATION_PROMPT_TEMPLATE = """Вы — концентратор знаний по управлению проектами.
Ваша задача: прочитать методические материалы из выбранных скиллов (приведены ниже полностью) и извлечь ТОЛЬКО то, что нужно для ответа на конкретный вопрос пользователя. Сформируйте самодостаточный документ-план, который заменит исходную методичку в следующем цикле ReAct.

ВОПРОС ПОЛЬЗОВАТЕЛЯ:
{query}

ВЫБРАННЫЕ СКИЛЛЫ С ПОЛНОЙ МЕТОДИЧКОЙ (читайте внимательно — инструменты, виджеты, пороги — всё внутри):
{skills_content}

ВЫВОД — строгий JSON, без markdown-ограждений, без пояснений:
{{
  "concentrated_knowledge": "строка — извлечённая методичка, РЕЛЕВАНТНАЯ конкретному вопросу. Сохраняйте дословно конкретные пороги, таблицы решений, названия методов (Resource Smoothing, Leveling, Crashing, Fast Tracking) и пошаговые инструкции. Убирайте нерелевантные разделы. Цель — сжатие в 3-10 раз по сравнению с оригиналом.",
  "planned_tools": ["имя_инструмента_1", "имя_инструмента_2"],
  "planned_widgets": [
    {{
      "intent": "точное_имя_intent_из_скиллов_выше",
      "widget_type": "тип_как_объявлено_в_yaml_скилла",
      "title_hint": "предлагаемый заголовок виджета на языке вопроса пользователя",
      "rationale": "одно предложение, почему этот виджет подходит к вопросу"
    }}
  ],
  "plan": "строка — пошаговый план с номерами: (1) какой инструмент вызвать первым и с каким фильтром, (2) что проверить в observation, (3) ... промежуточные шаги ..., (N) какой виджет вывести в Final Answer. Ссылайтесь на planned_tools и planned_widgets по имени. План должен быть самодостаточным — читаемым без исходной методички."
}}

КРИТИЧЕСКИЕ ПРАВИЛА:
1. Имена инструментов и widget intents УЖЕ есть в методичке выше (разделы "Используемые MCP инструменты" и YAML-блок "available_widgets"). НЕ выдумывайте новые — берите ТОЛЬКО из того, что написано в скиллах.
2. НЕ перефразируйте числовые пороги и названия методов — копируйте дословно.
3. concentrated_knowledge должна быть КОНКРЕТНОЙ и ПРИМЕНИМОЙ, а не общими фразами.
4. Для кросс-доменных запросов интегрируйте знания из всех релевантных скиллов в ЕДИНЫЙ связный нарратив.
5. План должен покрывать: порядок сбора данных -> логику анализа -> выбор виджетов -> форму финального ответа.
6. Если выбранный widget intent неверный — рендеринг упадёт, поэтому выбирайте внимательно из YAML-объявлений.
7. Выводите ТОЛЬКО JSON-объект. Без markdown-ограждений, без текста до и после.
8. НЕТ trailing commas. НЕТ одинарных кавычек. НЕТ комментариев. Строгий JSON.
9. УСЛОВНЫЕ/ЗАВИСИМЫЕ ИНСТРУМЕНТЫ — СОХРАНЯЙТЕ ИХ. Если скилл описывает инструмент Б, который вызывается только при выполнении некоторого условия в данных, полученных от другого инструмента А (например, "если перегрузка системная → вызови get_skill_gap"; "если CPI < 0.9 в течение 3+ периодов → вызови analyze_kpi_trends"), вы ОБЯЗАНЫ:
   - включить Б в planned_tools вместе с А (не выбрасывать Б как "нерелевантный");
   - сохранить if-then логику ДОСЛОВНО в concentrated_knowledge (само условие, а не парафраз);
   - отразить условный вызов как ВЕТВЛЕНИЕ в плане, например: "Шаг 3: ЕСЛИ в observation выполняется условие X → вызвать Б; ИНАЧЕ перейти к Шагу 4".
   Вопрос пользователя редко покрывает всю суть скилла — судите по вопросу: если вопрос МОЖЕТ триггерить условие (например, вопрос о загрузке ресурсов может выявить системную перегрузку → оставляем get_skill_gap), то условный инструмент и логика его триггера ОБЯЗАНЫ остаться. Опускайте условный инструмент только если вопрос пользователя ЯВНО НЕ МОЖЕТ триггерить его условие (например, вопрос о бюджетном расписании не может триггерить условие перегрузки персонала → убираем get_skill_gap).
   Это правило ПЕРЕОПРЕДЕЛЯЕТ цель сжатия "убирайте нерелевантные разделы": условная логика никогда не "нерелевантна", если вопрос может её триггерить.
10. ВЫБОР ВИДЖЕТОВ — СОПОСТАВЛЯЙТЕ ОПИСАНИЯ С ВОПРОСОМ. В YAML-блоке available_widgets у каждого intent есть поле description, объясняющее, КОГДА этот виджет применять. Внимательно прочитайте каждое description и включайте в planned_widgets те виджеты, чей use case (описанный в description) совпадает с вопросом пользователя. Например:
    - вопрос "кто свободен?" → виджет с description "Используй, когда нужно ответить на вопрос КТО свободен"
    - вопрос "кто может взять новую задачу?" → виджет с description "Используй, когда нужно найти свободные ресурсы для новой задачи"
    - вопрос "какая роль — узкое место?" → виджет с description "Используй, когда нужно ответить на вопрос КАКАЯ РОЛЬ является узким местом"
    НЕ оставляйте planned_widgets пустым, если в скилле есть виджет, description которого прямо отвечает на вопрос пользователя. Пустой planned_widgets означает, что LLM в ReAct не получит инструкцию по виджету и может опечататься в имени intent-а.
"""

def _extract_json_block(text: str) -> str:
    """ Извлекает первый сбалансированный {...} блок из текста LLM.
    Игнорирует markdown-фенсинги и сопутствующий текст """
    # Снимаем fensinги
    text = text.strip()
    if text.startswith("```"):
        # Убираем первую строку (```json или ```)
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        # Убираем последний ```
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    # Балансировка скобок: ищем первый '{' и парный '}'
    start = text.find("{")
    if start == -1:
        # Нет JSON вообще - вернём как есть, пусть json.loads выдаст ошибку
        return text  
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    # Скобки не сбалансированы - вернём что есть
    return text[start:]

def _strip_artifacts(text: str) -> str:
    """ Удаляет типичные LLM-артефакты в JSON: trailing commas, комментарии """
    # Убираем однострочные комментарии // ... (только вне строк)
    out_lines = []
    in_string = False
    escape = False
    for line in text.split("\n"):
        new_line = []
        i = 0
        while i < len(line):
            ch = line[i]
            if escape:
                escape = False
                new_line.append(ch)
                i += 1
                continue
            if ch == "\\":
                escape = True
                new_line.append(ch)
                i += 1
                continue
            if ch == '"':
                in_string = not in_string
                new_line.append(ch)
                i += 1
                continue
            if not in_string and ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
                # Комментарий до конца строки
                break
            new_line.append(ch)
            i += 1
        out_lines.append("".join(new_line))
    text = "\n".join(out_lines)
    # Убираем trailing commas: ,} или ,] (с пробелами/переносами между)
    text = re.sub(r",\s*([}\]])", r"\1", text)
    return text

def _fix_underscore_spaces_in_keys(text: str) -> str:
    """ Чистит пробелы в ключах вида "data_ rows" -> "data_rows" """
    return re.sub(
        r'"([^"]*)"',
        lambda m: '"' + re.sub(r'_\s+', '_', m.group(1)) + '"',
        text)


def _robust_json_parse(text: str) -> Optional[Dict[str, Any]]:
    """ Многоэтапный парсер JSON. Возвращает None, если совсем не вышло """
    # Шаг 1: fensinги + балансировка скобок
    block = _extract_json_block(text)
    # Шаг 2: чистим артефакты
    block = _strip_artifacts(block)
    # Шаг 3: фиксим пробелы в ключах
    block = _fix_underscore_spaces_in_keys(block)
    # Шаг 4: пробуем распарсить
    try:
        result = json.loads(block)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass
    # Шаг 5: запасной вариант - извлекаем поля по regex
    # (грубая страховка, когда LLM вернула почти-JSON)
    extracted: Dict[str, Any] = {}
    # Strings: "concentrated_knowledge": "..."
    for key in ("concentrated_knowledge", "plan"):
        m = re.search(
            r'"' + key + r'"\s*:\s*"((?:[^"\\]|\\.)*)"',
            block,
            re.DOTALL)
        if m:
            # Снимаем эскейпы \n, \", \\
            raw = m.group(1)
            extracted[key] = raw.encode("utf-8").decode("unicode_escape")
    # Arrays: "planned_tools": ["a", "b"]
    m = re.search(r'"planned_tools"\s*:\s*\[([^\]]*)\]', block, re.DOTALL)
    if m:
        items = re.findall(r'"([^"]*)"', m.group(1))
        extracted["planned_tools"] = items
    # Arrays of objects: planned_widgets (упрощённо - только intent-ы)
    widget_intents = []
    for m in re.finditer(
        r'"intent"\s*:\s*"([^"]*)"', block):
        widget_intents.append(m.group(1))
    if widget_intents:
        extracted["_regex_widget_intents"] = widget_intents
    if not extracted:
        return None
    if "planned_tools" not in extracted:
        extracted["planned_tools"] = []
    if "planned_widgets" not in extracted:
        extracted["planned_widgets"] = [
            {"intent": name} for name in widget_intents]
    if "concentrated_knowledge" not in extracted:
        extracted["concentrated_knowledge"] = ""
    if "plan" not in extracted:
        extracted["plan"] = ""
    return extracted

class SkillConcentrator:

    def __init__(
        self,
        llm_client: OpenAI,
        llm_model_name: str,
        structure):
        self.llm_client = llm_client
        self.llm_model_name = llm_model_name
        self.structure = structure

    async def concentrate(
        self,
        query: str,
        selected_plugins: List[str],
        active_tools: List[Dict[str, Any]]) -> ConcentratedContext:
        # Собираем полный текст методички + каталог виджетов
        skills_content_parts: List[str] = []
        widgets_catalog: List[Dict[str, Any]] = []
        for plugin in self.structure.plugins:
            if plugin["name"] not in selected_plugins:
                continue
            for skill in plugin["skills"]:
                skills_content_parts.append(
                    f"=== Skill: {skill['name']} (Plugin: {plugin['name']}) ===\n"
                    f"{skill['instructions']}")
                for widget_def in skill.get("available_widgets", []):
                    w_type = widget_def.get("type", "")
                    for intent in widget_def.get("intents", []):
                        intent_name = intent.get("name")
                        if intent_name:
                            widgets_catalog.append({
                                "intent": intent_name,
                                "widget_type": w_type,
                                "description": intent.get("description", ""),
                                # Вербатим, не отдаём LLM
                                "config": intent.get("config", {}),  
                                "source_skill": skill["name"]})
        skills_content = "\n\n".join(skills_content_parts)
        if not skills_content:
            return ConcentratedContext(fallback_used=True)
        prompt = CONCENTRATION_PROMPT_TEMPLATE.format(
            query=query,
            skills_content=skills_content)
        loop = asyncio.get_event_loop()

        def sync_call():
            return self.llm_client.chat.completions.create(
                model=self.llm_model_name,
                messages=[{"role": "user", "content": prompt}],
                temperature=_TEMPERATURE,
                max_tokens=_MAX_TOKENS)

        try:
            response = await loop.run_in_executor(None, sync_call)
            raw = response.choices[0].message.content.strip()
        except Exception as e:
            print(f"[Concentrator] LLM error: {e}. Fallback to full context.")
            return self._build_fallback_context(skills_content, selected_plugins)
        parsed = _robust_json_parse(raw)
        if parsed is None:
            print(f"[Concentrator] JSON parse failed even after robust cleanup.")
            print(f"[Concentrator] RAW OUTPUT (first 800 chars):")
            print(raw[:800])
            print(f"[Concentrator] --- END RAW ---")
            return self._build_fallback_context(skills_content, selected_plugins, raw)
        # Верификация виджетов: подтягиваем оригинальные config-ы вербатим.
        # LLM-концентратор выбрала эти виджеты как релевантные вопросу -> REQUIRED
        intent_index = {w["intent"]: w for w in widgets_catalog}
        verified_widgets: List[Dict[str, Any]] = []
        for pw in parsed.get("planned_widgets", []):
            intent_name = pw.get("intent") if isinstance(pw, dict) else None
            if intent_name and intent_name in intent_index:
                original = intent_index[intent_name]
                verified_widgets.append({
                    "intent": intent_name,
                    "widget_type": original["widget_type"],
                    "config": original["config"], # ВЕРБАТИМ из SKILL.md
                    "description": original["description"], # Описание 1:1 из SKILL.md
                    "source_skill": original["source_skill"], # Из какого скилла
                    "title_hint": pw.get("title_hint", "") if isinstance(pw, dict) else "",
                    "rationale": pw.get("rationale", "") if isinstance(pw, dict) else "",
                    "is_special": is_special_widget(original["widget_type"]),
                    "required": True}) # LLM выбрала -> обязательно генерировать
            else:
                print(
                    f"[Concentrator] WARN: LLM picked unknown intent "
                    f"'{intent_name}', dropped.")
        # FORCE-INCLUDE ОСОБЫХ ВИДЖЕТОВ (action_card и др).
        # Независимо от решения LLM-концентратора, всегда пробрасываем
        # виджеты особых типов из выбранных скиллов - как AVAILABLE (не REQUIRED).
        # LLM в ReAct сама решит, какой из них релевантен вопросу, по description.
        # Если LLM-концентратор уже выбрала какой-то action_card - не дублируем
        already_planned_intents = {w["intent"] for w in verified_widgets}
        force_included = []
        for plugin in self.structure.plugins:
            if plugin["name"] not in selected_plugins:
                continue
            for skill in plugin["skills"]:
                for widget_def in skill.get("available_widgets", []):
                    w_type = widget_def.get("type", "")
                    if w_type not in SPECIAL_WIDGET_TYPES:
                        continue
                    for intent in widget_def.get("intents", []):
                        intent_name = intent.get("name")
                        if not intent_name or intent_name in already_planned_intents:
                            continue
                        verified_widgets.append({
                            "intent": intent_name,
                            "widget_type": w_type,
                            "config": intent.get("config", {}),
                            "description": intent.get("description", ""), # 1:1 из SKILL.md
                            "source_skill": skill["name"], # из какого скилла
                            "title_hint": "",
                            "rationale": "FORCE-INCLUDED (special widget type)",
                            "is_special": True, # особый виджет -> модельный
                            "required": False}) # AVAILABLE: LLM решает по релевантности
                        already_planned_intents.add(intent_name)
                        force_included.append(intent_name)
                        print(
                            f"[Concentrator] FORCE-INCLUDE особый виджет: "
                            f"{intent_name} (type: {w_type}) из скилла {skill['name']}")
        if force_included:
            print(f"[Concentrator] Принудительно добавлено особых виджетов: "
                f"{force_included}")
        # Фильтрация planned_tools: оставляем только реальные
        valid_tool_names = {t["name"] for t in active_tools}
        planned_tools = [
            t for t in parsed.get("planned_tools", []) if t in valid_tool_names]
        # Логирование степени сжатия
        orig_size = len(skills_content)
        new_size = (
            len(parsed.get("concentrated_knowledge", ""))
            + len(parsed.get("plan", "")))
        ratio = (new_size / orig_size * 100) if orig_size else 0
        print(f"[Concentrator] Сжатие: {orig_size} -> {new_size} символов "
            f"({ratio:.0f}% от оригинала)")
        print(f"[Concentrator] Запланировано инструментов: {planned_tools}")
        print(f"[Concentrator] Запланировано виджетов: "
            f"{[w['intent'] for w in verified_widgets]}")

        return ConcentratedContext(
            concentrated_knowledge=parsed.get("concentrated_knowledge", "").strip(),
            planned_tools=planned_tools,
            planned_widgets=verified_widgets,
            plan=parsed.get("plan", "").strip(),
            raw_llm_output=raw)

    # Fallback: концентрация не удалась - отдаём полный контекст
    def _build_fallback_context(
        self,
        skills_content: str,
        selected_plugins: List[str] = None,
        raw_output: str = "") -> ConcentratedContext:
        # Force-include особых виджетов даже в fallback - как AVAILABLE.
        # ВАЖНО: config НЕ дублируем - он уже есть в skills_content (полный YAML
        # available_widgets). LLM найдёт его там по имени intent. Здесь отдаём
        # только идентификацию (intent, widget_type) + description для выбора.
        forced_widgets: List[Dict[str, Any]] = []
        if selected_plugins:
            for plugin in self.structure.plugins:
                if plugin["name"] not in selected_plugins:
                    continue
                for skill in plugin["skills"]:
                    for widget_def in skill.get("available_widgets", []):
                        w_type = widget_def.get("type", "")
                        if w_type not in SPECIAL_WIDGET_TYPES:
                            continue
                        for intent in widget_def.get("intents", []):
                            intent_name = intent.get("name")
                            if intent_name:
                                forced_widgets.append({
                                    "intent": intent_name,
                                    "widget_type": w_type,
                                    "config": {},  # Пусто - config в skills_content
                                    "description": intent.get("description", ""),
                                    "source_skill": skill["name"],
                                    "title_hint": "",
                                    "rationale": "FORCE-INCLUDED (fallback, special widget)",
                                    "is_special": True,
                                    "required": False})
                                print(f"[Concentrator] FORCE-INCLUDE (fallback) особый виджет: "
                                    f"{intent_name} (type: {w_type}) — без config (он в skills_content)")
        return ConcentratedContext(
            concentrated_knowledge=skills_content,
            planned_tools=[],
            planned_widgets=forced_widgets,
            plan="",
            fallback_used=True,
            raw_llm_output=raw_output)

    # Разбить planned_widgets на 2 группы
    @staticmethod
    def split_widgets_by_type(
        planned_widgets: List[Dict[str, Any]]) -> tuple:
        """ Возвращает (system_widgets, model_widgets).
        system_widgets - НЕ особые: рендерятся через <!-- SYSTEM_WIDGET --> placeholder
        model_widgets - особые (action_card и др.): требуют LLM-генерации JSON-блока """
        system_w = []
        model_w = []
        for w in planned_widgets:
            if w.get("is_special", False):
                model_w.append(w)
            else:
                system_w.append(w)
        return system_w, model_w