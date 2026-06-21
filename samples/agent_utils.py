from __future__ import annotations
import json
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

def extract_json_block(text: str) -> str:
    """ Извлекает первый сбалансированный {...} блок из текста LLM.
    Игнорирует markdown-ограждения и сопутствующий текст """
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    start = text.find("{")
    if start == -1:
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
    return text[start:]

def strip_artifacts(text: str) -> str:
    """ Удаляет типичные LLM-артефакты в JSON: trailing commas, однострочные // комментарии """
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
                break
            new_line.append(ch)
            i += 1
        out_lines.append("".join(new_line))
    text = "\n".join(out_lines)
    text = re.sub(r",\s*([}\]])", r"\1", text)
    return text

def fix_underscore_spaces_in_keys(text: str) -> str:
    """ Чистит пробелы в ключах вида "data_ rows" -> "data_rows" """
    return re.sub(
        r'"([^"]*)"',
        lambda m: '"' + re.sub(r'_\s+', '_', m.group(1)) + '"',
        text)

def robust_json_parse(text: str) -> Optional[Dict[str, Any]]:
    """ Многоэтапный парсер JSON. Возвращает None, если совсем не вышло """
    block = extract_json_block(text)
    block = strip_artifacts(block)
    block = fix_underscore_spaces_in_keys(block)
    try:
        result = json.loads(block)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass
    # Запасной вариант - regex-извлечение полей
    extracted: Dict[str, Any] = {}
    for key in ("concentrated_knowledge", "plan"):
        m = re.search(
            r'"' + key + r'"\s*:\s*"((?:[^"\\]|\\.)*)"',
            block,
            re.DOTALL)
        if m:
            raw = m.group(1)
            try:
                extracted[key] = raw.encode("utf-8").decode("unicode_escape")
            except (UnicodeDecodeError, UnicodeEncodeError):
                extracted[key] = raw
    m = re.search(r'"planned_tools"\s*:\s*\[([^\]]*)\]', block, re.DOTALL)
    if m:
        items = re.findall(r'"([^"]*)"', m.group(1))
        extracted["planned_tools"] = items
    widget_intents = []
    for m in re.finditer(r'"intent"\s*:\s*"([^"]*)"', block):
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

def parse_llm_response(text: str) -> Dict[str, Any]:
    """ Парсит ответ LLM без регулярных выражений, на базе стейт-машины.
    Возвращает dict с типом:
      - {"type": "final_answer", "answer": ...}
      - {"type": "action", "thought": ..., "action": ..., "action_input": ...}
      - {"type": "hallucinated_observation", "text": ...}
      - {"type": "thought", "text": ...} """
    lines = text.split('\n')
    if lines and lines[0].strip().lower().startswith('question:'):
        lines = lines[1:]
        text = '\n'.join(lines)
    thought_parts: List[str] = []
    action = ""
    action_input_parts: List[str] = []
    state = 'none'
    for line in lines:
        stripped = line.strip()
        if not stripped and state != 'action_input':
            continue
        lower = stripped.lower()
        if lower.startswith("final answer:") or lower.startswith("final answer :"):
            idx = text.lower().find("final answer")
            if idx != -1:
                colon_idx = text.find(":", idx)
                answer_text = text[colon_idx + 1:].strip() if colon_idx != -1 else text[idx + len("final answer"):].strip()
            else:
                answer_text = stripped
            return {"type": "final_answer", "answer": answer_text}
        if state == 'action_input':
            if lower.startswith("action:") or lower.startswith("action input:"):
                continue
            action_input_parts.append(stripped)
            continue
        if lower.startswith("observation:"):
            if action:
                return {"type": "action",
                        "thought": "\n".join(thought_parts).strip(),
                        "action": action.strip(),
                        "action_input": "\n".join(action_input_parts).strip()}
            return {"type": "hallucinated_observation", "text": text}
        if lower.startswith("action input:") or lower.startswith("action input :"):
            state = 'action_input'
            parts = stripped.split(":", 1)
            if len(parts) > 1 and parts[1].strip():
                action_input_parts.append(parts[1].strip())
            continue
        if lower.startswith("action:") or lower.startswith("action :") or lower == "action":
            if state != 'action':
                state = 'action'
                parts = stripped.split(":", 1)
                if len(parts) > 1 and parts[1].strip():
                    candidate = parts[1].strip()
                    # LLM порой путается и пишет виджет как Action - конвертируем в Final Answer
                    if (candidate.startswith("<!--")
                            or candidate.startswith("SYSTEM_WIDGET")
                            or "<!-- SYSTEM_WIDGET" in text):
                        fa_parts = []
                        if thought_parts:
                            fa_parts.append("\n".join(thought_parts).strip())
                        fa_parts.append(candidate)
                        full_action_text = text[text.find("Action:"):]
                        if "<!-- SYSTEM_WIDGET" in full_action_text:
                            thought_idx = text.lower().find("thought:")
                            if thought_idx != -1:
                                raw_answer = text[thought_idx:].strip()
                                cleaned_lines = []
                                for ln in raw_answer.split("\n"):
                                    ls = ln.strip()
                                    ll = ls.lower()
                                    if ll.startswith("thought:"):
                                        content = ls[len("thought:"):].strip()
                                        if content:
                                            cleaned_lines.append(content)
                                    elif ll.startswith("action:"):
                                        content = ls[len("action:"):].strip()
                                        if content:
                                            cleaned_lines.append(content)
                                    elif ll.startswith("action input:"):
                                        continue
                                    else:
                                        cleaned_lines.append(ln)
                                cleaned_answer = "\n".join(cleaned_lines).strip()
                                return {"type": "final_answer", "answer": cleaned_answer}
                        return {"type": "final_answer",
                                "answer": "\n\n".join(fa_parts).strip()}
                    action = candidate.split()[0] if candidate.split() else ""
            continue
        if lower.startswith("thought:") or lower.startswith("thought :") or lower == "thought":
            state = 'thought'
            parts = stripped.split(":", 1)
            if len(parts) > 1 and parts[1].strip():
                thought_parts.append(parts[1].strip())
            continue
        if state == 'thought':
            thought_parts.append(stripped)
        elif state == 'action':
            if not action:
                tokens = stripped.split()
                action = tokens[0] if tokens else ""
        elif state == 'action_input':
            action_input_parts.append(stripped)
    if action:
        return {"type": "action",
                "thought": "\n".join(thought_parts).strip(),
                "action": action.strip(),
                "action_input": "\n".join(action_input_parts).strip()}
    return {"type": "thought", "text": text}

def detect_loop(history: List[Dict], threshold: int = 3) -> bool:
    """ Обнаруживает зацикливание ReAct (для history-режима).
    Критерий 1: похожие первые 100 символов у последних N Thought.
    Критерий 2: одинаковый Action + Action Input у последних N сообщений """
    if len(history) < threshold * 2:
        return False
    recent_assistant_msgs = [
        msg["content"] for msg in history[-threshold * 2:]
        if msg["role"] == "assistant"][-threshold:]
    if len(recent_assistant_msgs) < threshold:
        return False
    first_msg_prefix = recent_assistant_msgs[0][:100]
    similar_count = sum(1 for msg in recent_assistant_msgs if msg[:100] == first_msg_prefix)
    if similar_count >= threshold:
        return True

    def extract_action_sig(msg: str) -> str:
        action = ""
        action_input = ""
        for line in msg.split("\n"):
            ls = line.strip().lower()
            if ls.startswith("action:") and not action:
                parts = line.strip().split(":", 1)
                if len(parts) > 1:
                    tokens = parts[1].strip().split()
                    action = tokens[0] if tokens else ""
            elif ls.startswith("action input:") and not action_input:
                parts = line.strip().split(":", 1)
                if len(parts) > 1:
                    action_input = parts[1].strip()
        return f"{action}||{action_input}"

    sigs = [extract_action_sig(msg) for msg in recent_assistant_msgs]
    if all(s for s in sigs) and len(set(sigs)) == 1:
        return True
    return False

def detect_hallucinations(final_answer: str, observations: List[str]) -> List[str]:
    """ Наивная проверка: не выдумала ли LLM имена, которых нет в данных """
    mentioned_names = set(re.findall(r'\b([А-ЯA-Z][а-яa-z]+\s+[А-ЯA-Z]\.)\b', final_answer))
    data_names = set()
    for obs in observations:
        data_names.update(re.findall(r'\b([А-ЯA-Z][а-яa-z]+\s+[А-ЯA-Z]\.)\b', obs))
    return list(mentioned_names - data_names)

# Stateless-режим: парсер плана.
# Шаблоны шагов плана:
#   "Step 1: Call get_risk_register with args {"project_name": "ERP"} - причина"
#   "1. get_risk_register({"project_name": "ERP"}) - причина"
#   "Step 3: Final Answer"
_STEP_TOOL_RE = re.compile(
    r'^\s*(?:step\s*)?(\d+)[\.\):]\s*'  # номер шага
    r'(?:call\s+)?([A-Za-z_][\w]*)\s*'   # имя инструмента
    r'(?:\s+with\s+args?\s*)?'  # опц. "with args"
    r'(?:(\{[^}]*\}))?' # JSON-аргументы (опц.)
    r'\s*(?:[-—–]\s*(.*))?$'    # причина (опц.)
    , re.IGNORECASE)
_STEP_FINAL_RE = re.compile(
    r'^\s*(?:step\s*)?(\d+)[\.\):]\s*'
    r'(?:final\s*answer|prepare\s+final\s+answer|prepare\s+the\s+final\s+answer|'
    r'сформировать\s+финальный\s+ответ|подготовить\s+финальный\s+ответ|финальный\s+ответ)',
    re.IGNORECASE)

def parse_plan_steps(plan_text: str) -> List[Dict[str, Any]]:
    """ Разбирает блок `Plan:` в список шагов.
    Возвращает список словарей:
      - {"step": 1, "kind": "tool_call", "tool": "get_risk_register",
         "args": {...}, "reason": "..."}
      - {"step": 3, "kind": "final_answer", "reason": "..."}
    Нераспознанные строки игнорируются (но не обрывают парсинг) """
    steps: List[Dict[str, Any]] = []
    current_step: Optional[Dict[str, Any]] = None
    for raw_line in plan_text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        m_final = _STEP_FINAL_RE.match(line)
        if m_final:
            if current_step:
                steps.append(current_step)
            current_step = {
                "step": int(m_final.group(1)),
                "kind": "final_answer",
                "reason": ""}
            # TODO: 10 - в константы
            if len(steps) >= 10:
                break
            continue
        m_tool = _STEP_TOOL_RE.match(line)
        if m_tool:
            if current_step:
                steps.append(current_step)
            num = int(m_tool.group(1))
            tool = m_tool.group(2)
            args_str = m_tool.group(3) or "{}"
            reason = (m_tool.group(4) or "").strip()
            try:
                args = json.loads(args_str)
            except json.JSONDecodeError:
                args = {}
            current_step = {
                "step": num,
                "kind": "tool_call",
                "tool": tool,
                "args": args,
                "reason": reason}
            # TODO: 10 - в константы
            if len(steps) >= 10:
                break
            continue
        # Многострочная причина или продолжение
        if current_step and not line.strip().startswith(("#", "//")):
            if current_step.get("reason"):
                current_step["reason"] += " " + line.strip()
            else:
                current_step["reason"] = line.strip()
    # TODO: 10 - в константы
    if current_step and len(steps) < 10:
        steps.append(current_step)
    return steps

def extract_plan_block(llm_text: str) -> Tuple[str, str]:
    """ Разделяет текст ответа LLM на блок `Plan:` и остальное.
    Возвращает (plan_text, rest_text) """
    # Ищем заголовок "Plan:" (case-insensitive, поддержка кириллицы "План:")
    pattern = re.compile(
        r'(?im)^\s*(plan|план)\s*:\s*$')
    m = pattern.search(llm_text)
    if not m:
        # Возможен inline: "Plan: 1. ... 2. ..." - ищем заголовок в строке
        pattern_inline = re.compile(r'(?im)^\s*(plan|план)\s*:\s*(.+)$')
        m_inline = pattern_inline.search(llm_text)
        if m_inline:
            plan_start = m_inline.start()
            # План длится до первой пустой строки после заголовка ИЛИ до Thought:/Action:/Final Answer:
            end_pattern = re.compile(
                r'(?im)^\s*(thought|рассуждение|действие|action|final\s*answer|финальный\s*ответ)\s*:')
            end_m = end_pattern.search(llm_text, m_inline.end())
            plan_end = end_m.start() if end_m else len(llm_text)
            plan_text = llm_text[plan_start:plan_end].strip()
            rest_text = (llm_text[:plan_start] + "\n" + llm_text[plan_end:]).strip()
            return plan_text, rest_text
        return "", llm_text
    plan_start = m.start()
    # План длится до первого заголовка вне плана
    end_pattern = re.compile(
        r'(?im)^\s*(thought|рассуждение|действие|action|final\s*answer|финальный\s*ответ)\s*:')
    end_m = end_pattern.search(llm_text, m.end())
    plan_end = end_m.start() if end_m else len(llm_text)
    plan_text = llm_text[plan_start:plan_end].strip()
    rest_text = (llm_text[:plan_start] + "\n" + llm_text[plan_end:]).strip()
    return plan_text, rest_text

def make_call_signature(tool: str, args: Dict[str, Any]) -> str:
    """ Стабильная сигнатура вызова для сравнения на дубликат """
    return f"{tool}||{json.dumps(args, sort_keys=True, ensure_ascii=False)}"

def is_consecutive_duplicate(
        proposed_tool: str,
        proposed_args: Dict[str, Any],
        recent_calls_stack: List[Tuple[str, Dict[str, Any]]]) -> bool:
    """ True, если предлагаемый вызов совпадает с cсамым свежим в стеке
    (т.е. это был бы подряд второй одинаковый вызов).
    Стек хранит последние 2 вызова; проверяем только stack[0] (вершину) """
    if not recent_calls_stack:
        return False
    top_tool, top_args = recent_calls_stack[0]
    return (top_tool == proposed_tool
            and make_call_signature(top_tool, top_args)
            == make_call_signature(proposed_tool, proposed_args))

def clean_broken_json(text: str) -> str:
    """ Удаляет оборванные ```json блоки, если LLM упёрлась в max_tokens """
    cleaned = re.sub(r"```json\s*[\s\S]*?(?=```)",
        lambda m: m.group(0) if text.count("```") % 2 == 0 else "",
        text)
    cleaned = re.sub(r"```json\s*[\s\S]*?(?<!```)$", "", text)
    return cleaned.strip()

def expand_widget_descriptors(
        final_answer: str,
        render_widget_fn: Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]) -> str:
    """ Постобработка виджетов в Final Answer.
    Ищет JSON-блоки с полем "widget_type" двумя способами:
      1. Fenced: ```json ... ``` (стандартный формат)
      2. Bare: { ... "widget_type": ... ... } (LLM иногда забывает ограждения).
    Каждый найденный дескриптор прогоняется через render_widget_fn и заменяется
    на отрендеренный envelope в ```json ... ``` формате """
    
    def fix_underscore_spaces(m: re.Match) -> str:
        inner = m.group(1)
        fixed = re.sub(r'_\s+', '_', inner)
        return f'"{fixed}"'

    def process_raw_json(raw: str) -> Optional[str]:
        raw = raw.strip()
        raw = re.sub(r'"([^"]*)"', fix_underscore_spaces, raw)
        try:
            descriptor = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"[WidgetRenderer] JSONDecodeError: {e} | raw[:200]: {raw[:200]}")
            return None
        if "widget_type" not in descriptor:
            return None
        if descriptor.get("widget_type") == "echarts":
            # Уже отрендеренный бэкендом график не трогаем
            return None
        envelope = render_widget_fn(descriptor)
        if envelope is None:
            print(f"[WidgetRenderer] render_widget=None для intent={descriptor.get('intent')}")
            return None
        expanded = json.dumps(envelope, ensure_ascii=False, indent=2)
        print(f"[WidgetRenderer] OK: {descriptor.get('widget_type')}/{descriptor.get('intent')} -> {len(expanded)} chars")
        return f"```json\n{expanded}\n```"

    # Fenced-блоки
    json_block_re = re.compile(r"```json\s*([\s\S]*?)```", re.MULTILINE)

    def replace_fenced(match: re.Match) -> str:
        rendered = process_raw_json(match.group(1))
        return rendered if rendered is not None else match.group(0)
    result = json_block_re.sub(replace_fenced, final_answer)

    # Bare JSON-блоки
    bare_blocks: List[Tuple[int, int, str]] = []
    fence_re = re.compile(r"```[a-zA-Z0-9]*\n?")
    fence_spans: List[Tuple[int, int]] = []
    # ИСПРАВЛЕНО: в оригинале была синтаксическая ошибка (пропущена '[')
    fence_starts = [m.start() for m in fence_re.finditer(result)]
    for k in range(0, len(fence_starts) - 1, 2):
        open_start = fence_starts[k]
        close_start = fence_starts[k + 1]
        fence_spans.append((open_start, close_start))

    def in_fence(pos: int) -> bool:
        for fs, fe in fence_spans:
            if fs <= pos < fe:
                return True
        return False

    i = 0
    text = result
    while i < len(text):
        ch = text[i]
        if ch != "{":
            i += 1
            continue
        if in_fence(i):
            i += 1
            continue
        depth = 0
        in_string = False
        escape = False
        j = i
        while j < len(text):
            c = text[j]
            if escape:
                escape = False
                j += 1
                continue
            if c == "\\":
                escape = True
                j += 1
                continue
            if c == '"':
                in_string = not in_string
                j += 1
                continue
            if in_string:
                j += 1
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    raw = text[i:j + 1]
                    if '"widget_type"' in raw or '"widget_type ' in raw:
                        bare_blocks.append((i, j + 1, raw))
                    i = j + 1
                    break
            j += 1
        else:
            break
        if depth != 0:
            break

    for start, end, raw in reversed(bare_blocks):
        rendered = process_raw_json(raw)
        if rendered is not None:
            result = result[:start] + rendered + result[end:]
    return result