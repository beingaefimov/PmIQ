""" Этап концентрации знаний.
Вызывается после маршрутизации и до запуска ReAct-цикла.
Принимает выбранные плагины с их полной методичкой, извлекает только ту её
часть, которая нужна для конкретного вопроса пользователя, и формирует
самодостаточный план:
    concentrated_knowledge - сжатая методичка, релевантная вопросу
    planned_tools - список инструментов в порядке вызова
    planned_widgets - выбранные виджеты с ОРИГИНАЛЬНЫМИ config-ами
    plan - пошаговый план сбора данных и ответа """

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from openai import OpenAI

# Особые виджеты, которые всегда пробрасываются в planned_widgets,
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
1. Имена инструментов и widget intents УЖЕ есть в методичке выше. НЕ выдумывайте новые — берите ТОЛЬКО из того, что написано в скиллах.
   **ВАЖНОЕ РАЗЛИЧЕНИЕ:**
   - `planned_tools` — это ИМЕНА ИНСТРУМЕНТОВ (как `get_risk_register`, `calculate_evm`, `get_ncr_status`). Они перечислены в разделе «Используемые MCP инструменты» каждого скилла.
   - `planned_widgets[].intent` — это ИМЕНА INTENTS ВИДЖЕТОВ (как `risk_matrix_priority`, `evm_curves`, `cpi_spi_trend`). Они перечислены в YAML-блоке `available_widgets` под полем `name` у каждого intent-а.
   - **ИМЯ ИНСТРУМЕНТА И ИМЯ INTENT-А — ЭТО РАЗНЫЕ ВЕЩИ.** Не подставляйте имя инструмента в поле `intent`. Если в скилле `available_widgets` нет ни одного intent-а, релевантного вопросу — оставьте `planned_widgets` пустым. Лучше пустой список, чем неверный intent.
   - Пример ПРАВИЛЬНОГО заполнения:
     ```json
     "planned_tools": ["get_risk_register", "simulate_risk_impact"],
     "planned_widgets": [
       {{"intent": "risk_matrix_priority", "widget_type": "ScatterChart",
        "title_hint": "Матрица рисков проекта ERP", "rationale": "Показывает топ-рисков по вероятности и воздействию"}}
     ]
     ```
   - Пример ОШИБОЧНОГО заполнения (intent = имя инструмента — ТАК НЕЛЬЗЯ):
     ```json
     "planned_widgets": [
       {{"intent": "get_ncr_status", "widget_type": "BarChart", ...}}
     ]
     ```
2. НЕ перефразируйте числовые пороги и названия методов — копируйте дословно.
3. concentrated_knowledge должна быть КОНКРЕТНОЙ и ПРИМЕНИМОЙ, а не общими фразами.
4. Для кросс-доменных запросов интегрируйте знания из всех релевантных скиллов в ЕДИНЫЙ связный нарратив.
5. План должен покрывать: порядок сбора данных -> логику анализа -> выбор виджетов -> форму финального ответа.
6. Если выбранный widget intent неверный — рендеринг упадёт, поэтому выбирайте внимательно из YAML-объявлений. Имя intent-а должно СОВПАДАТЬ с тем, что написано в `available_widgets[].intents[].name`.
7. Выводите ТОЛЬКО JSON-объект. Без markdown-ограждений, без текста до и после.
8. НЕТ trailing commas. НЕТ одинарных кавычек. НЕТ комментариев. Строгий JSON.
9. УСЛОВНЫЕ/ЗАВИСИМЫЕ ИНСТРУМЕНТЫ — СОХРАНЯЙТЕ ИХ. Если скилл описывает инструмент Б, который вызывается только при выполнении некоторого условия в данных, полученных от другого инструмента А, вы ОБЯЗАНЫ:
   - включить Б в planned_tools вместе с А (не выбрасывайте Б как "нерелевантный");
   - сохранить if-then логику ДОСЛОВНО в concentrated_knowledge (само условие, а не парафраз);
   - отразить условный вызов как ВЕТВЛЕНИЕ в плане.
10. ВЫБОР ВИДЖЕТОВ — СОПОСТАВЛЯЙТЕ ОПИСАНИЯ С ВОПРОСОМ. В YAML-блоке available_widgets у каждого intent есть поле description, объясняющее, КОГДА этот виджет применять. Внимательно прочитайте каждое description и включайте в planned_widgets те виджеты, чей use case (описанный в description) совпадает с вопросом пользователя. Если в скилле нет виджетов — НЕ выдумывайте их, оставьте planned_widgets пустым.
"""

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
        # Локальный импорт, чтобы избежать циклической зависимости
        from agent_utils import robust_json_parse

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
                                "config": intent.get("config", {}),
                                "source_skill": skill["name"]})
        skills_content = "\n\n".join(skills_content_parts)
        if not skills_content:
            return ConcentratedContext(fallback_used=True)
        prompt = CONCENTRATION_PROMPT_TEMPLATE.format(
            query=query, skills_content=skills_content)
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
        parsed = robust_json_parse(raw)
        if parsed is None:
            print("[Concentrator] JSON parse failed even after robust cleanup.")
            print(f"[Concentrator] RAW OUTPUT (first 800 chars):\n{raw[:800]}")
            return self._build_fallback_context(skills_content, selected_plugins, raw)

        # Верификация виджетов
        intent_index = {w["intent"]: w for w in widgets_catalog}
        verified_widgets: List[Dict[str, Any]] = []
        for pw in parsed.get("planned_widgets", []):
            intent_name = pw.get("intent") if isinstance(pw, dict) else None
            if intent_name and intent_name in intent_index:
                original = intent_index[intent_name]
                verified_widgets.append({
                    "intent": intent_name,
                    "widget_type": original["widget_type"],
                    "config": original["config"],
                    "description": original["description"],
                    "source_skill": original["source_skill"],
                    "title_hint": pw.get("title_hint", "") if isinstance(pw, dict) else "",
                    "rationale": pw.get("rationale", "") if isinstance(pw, dict) else "",
                    "is_special": is_special_widget(original["widget_type"]),
                    "required": True})
            else:
                print(f"[Concentrator] WARN: LLM picked unknown intent '{intent_name}', dropped.")

        # Force включаем особые виджеты
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
                            "description": intent.get("description", ""),
                            "source_skill": skill["name"],
                            "title_hint": "",
                            "rationale": "FORCE-INCLUDED (special widget type)",
                            "is_special": True,
                            "required": False})
                        already_planned_intents.add(intent_name)
                        force_included.append(intent_name)
                        print(f"[Concentrator] FORCE-INCLUDE: {intent_name} "
                              f"(type: {w_type}) из {skill['name']}")
        if force_included:
            print(f"[Concentrator] Принудительно добавлено особых виджетов: {force_included}")

        valid_tool_names = {t["name"] for t in active_tools}
        planned_tools = [t for t in parsed.get("planned_tools", []) if t in valid_tool_names]

        orig_size = len(skills_content)
        new_size = (len(parsed.get("concentrated_knowledge", ""))
                    + len(parsed.get("plan", "")))
        ratio = (new_size / orig_size * 100) if orig_size else 0
        print(f"[Concentrator] Сжатие: {orig_size} -> {new_size} символов ({ratio:.0f}%)")
        print(f"[Concentrator] Запланировано инструментов: {planned_tools}")
        print(f"[Concentrator] Запланировано виджетов: "
              f"{[w['intent'] for w in verified_widgets]}")

        return ConcentratedContext(
            concentrated_knowledge=parsed.get("concentrated_knowledge", "").strip(),
            planned_tools=planned_tools,
            planned_widgets=verified_widgets,
            plan=parsed.get("plan", "").strip(),
            raw_llm_output=raw)

    def _build_fallback_context(
            self,
            skills_content: str,
            selected_plugins: Optional[List[str]] = None,
            raw_output: str = "") -> ConcentratedContext:
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
                                    "config": {},
                                    "description": intent.get("description", ""),
                                    "source_skill": skill["name"],
                                    "title_hint": "",
                                    "rationale": "FORCE-INCLUDED (fallback, special widget)",
                                    "is_special": True,
                                    "required": False})
                                print(f"[Concentrator] FORCE-INCLUDE (fallback): "
                                      f"{intent_name} (type: {w_type})")
        return ConcentratedContext(
            concentrated_knowledge=skills_content,
            planned_tools=[],
            planned_widgets=forced_widgets,
            plan="",
            fallback_used=True,
            raw_llm_output=raw_output)

    @staticmethod
    def split_widgets_by_type(
            planned_widgets: List[Dict[str, Any]]) -> tuple:
        """ Возвращает (system_widgets, model_widgets).
        system_widgets - не особые: рендерятся через <!-- SYSTEM_WIDGET --> placeholder.
        model_widgets - особые (action_card и др.): требуют LLM-генерации JSON-блока """
        system_w = []
        model_w = []
        for w in planned_widgets:
            if w.get("is_special", False):
                model_w.append(w)
            else:
                system_w.append(w)
        return system_w, model_w