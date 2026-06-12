""" PM IQ Agent - Архитектура с мульти-плагиновой маршрутизацией (Уровень 1).

При проектировании агентных систем с большим количеством инструментов (30+) 
существует дилемма: "Полнота контекста" vs "Перегрузка контекста (Context Bloat)".

Известные подходы:

Уровень 1: Мульти-плагиновый роутер (текущая реализация)
- Как работает: LLM сначала анализирует запрос и выбирает 1-3 релевантных плагина. 
  Затем в ReAct-промт загружаются инструменты и инструкции только этих плагинов.
- Плюсы: Резкая экономия токенов (вместо 30 инструментов в промпте попадает 4-8), 
  высокая надежность на локальных моделях (7B-8B), поддержка кросс-доменных запросов 
  (например, "задержка и бюджет" выберет 2 плагина).
- Минусы: Зависит от способности LLM корректно классифицировать запрос по доменам.
- Условия применения: Локальные LLM (llama.cpp), < 50 инструментов, чёткие границы доменов.

Уровкнь 1 bis: Добавить 1.2 уровень роутинга - после выбора плагина, LLM выбирает конкретный скилл внутри него.
Но только введя арбитра, оценщика, выбранного скиллса и если выбор одного не очевиден (ясно что он покрывает вопрос),
то оставлять все.

Уровень 2: Семантический поиск инструментов / RAG for Tools
- Как работает: Описания всех инструментов векторизуются (эмбеддинги). При запросе 
  выполняется семантический поиск (cosine similarity), и в промпт попадают топ-5 
  наиболее релевантных инструментов, независимо от того, в каком они плагине.
- Плюсы: Бесконечная масштабируемость (1000+ инструментов), не зависит от жестких 
  границ плагинов, находит инструменты по смыслу, а не по названию категории.
- Минусы: Требует локальной модели эмбеддингов, векторного хранилища (QDrant/Chroma/FAISS), 
  усложняет архитектуру.
- Условия применения: Продакшен-системы с > 50 инструментами, сильно пересекающимися доменами.

Уровень 3: Иерархия суб-агентов
- Как работает: Главный агент-диспетчер разбивает сложный запрос на подзадачи и 
  делегирует их специализированным суб-агентам (например, "Агент расписания" и "Агент рисков"), 
  которые общаются друг с другом.
- Плюсы: Глубочайшее рассуждение, автономность, решение сверхсложных задач.
- Минусы: Экспоненциальный рост затрат токенов, высокая задержка (latency), высокий 
  риск зацикливания или потери контекста.
- Условия применения: Мощные, желательно естественные, модели (GPT-4o, Claude 3.5 Sonnet), сложные 
  многоэтапные автономные workflow """

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import List, Dict, Any
import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import OpenAI

PM_IQ_ROOT = Path(__file__).parent.parent

# Настройки LLM
LLM_BASE_URL = "http://localhost:8080/v1"
LLM_MODEL_NAME = "local-model"

class PmIqStructureParser:
    def __init__(self, root_path: Path):
        self.root_path = root_path
        self.plugins_dir = root_path / "plugins"
        self.plugins: List[Dict[str, Any]] = []
    
    def scan_plugins(self) -> List[Dict[str, Any]]:
        if not self.plugins_dir.exists():
            raise ValueError(f"Директория плагинов не найдена: {self.plugins_dir}")
        
        for plugin_dir in sorted(self.plugins_dir.iterdir()):
            if not plugin_dir.is_dir() or plugin_dir.name.startswith('.'):
                continue
            
            plugin_info = self._parse_plugin(plugin_dir)
            if plugin_info:
                self.plugins.append(plugin_info)
        return self.plugins
    
    def _parse_plugin(self, plugin_dir: Path) -> Dict[str, Any]:
        plugin_info = {"name": plugin_dir.name, "path": plugin_dir, "skills": [], "mcp_config": None}
        
        mcp_config_file = plugin_dir / ".mcp.json"
        if mcp_config_file.exists():
            with open(mcp_config_file, 'r', encoding='utf-8') as f:
                plugin_info["mcp_config"] = json.load(f)
        
        skills_dir = plugin_dir / "skills"
        if skills_dir.exists():
            for skill_dir in sorted(skills_dir.iterdir()):
                if skill_dir.is_dir():
                    skill_info = self._parse_skill(skill_dir)
                    if skill_info:
                        plugin_info["skills"].append(skill_info)
        return plugin_info
    
    def _parse_skill(self, skill_dir: Path) -> Dict[str, Any]:
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            return None
        
        with open(skill_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        if not content.startswith('---'):
            return None
        
        parts = content.split('---', 2)
        if len(parts) < 3:
            return None
        
        frontmatter = yaml.safe_load(parts[1]) or {}
        return {
            "name": frontmatter.get("name", skill_dir.name),
            "description": frontmatter.get("description", ""),
            "instructions": parts[2].strip(),
            "path": skill_dir}

class PmIqAgent:
    def __init__(self):
        self.llm_client = OpenAI(base_url=LLM_BASE_URL, api_key="not-needed")
        self.structure = PmIqStructureParser(PM_IQ_ROOT)
        self.mcp_sessions: Dict[str, ClientSession] = {}
        self.all_tools: List[Dict[str, Any]] = []
        self._context_managers: List[tuple] = []
        
    async def initialize(self):
        print("Сканирование структуры PM IQ...")
        self.structure.scan_plugins()
        print(f"Найдено {len(self.structure.plugins)} плагинов")
        
        print("\nПодключение к MCP серверам (согласно .mcp.json)...")
        await self._connect_to_mcp_servers()
        print(f"Загружено {len(self.all_tools)} инструментов (внутренний реестр)")
    
    async def _connect_to_mcp_servers(self):
        for plugin in self.structure.plugins:
            if not plugin["mcp_config"]:
                continue
            
            mcp_servers = plugin["mcp_config"].get("mcpServers", {})
            for server_name, server_config in mcp_servers.items():
                try:
                    session = await self._connect_to_server(server_name, server_config)
                    if session:
                        self.mcp_sessions[server_name] = session
                        tools_result = await session.list_tools()
                        self.all_tools.extend([
                            {**tool.model_dump(), "server": server_name, "plugin": plugin["name"]}
                            for tool in tools_result.tools
                        ])
                except Exception as e:
                    print(f"Ошибка подключения к {server_name}: {e}")
    
    async def _connect_to_server(self, server_name: str, config: dict) -> ClientSession:
        command = config.get("command", "python3")
        args = config.get("args", [])
        env = config.get("env", {})
        
        server_params = StdioServerParameters(command=command, args=args, env=env)
        
        stdio_cm = stdio_client(server_params)
        read, write = await stdio_cm.__aenter__()
        
        session = ClientSession(read, write)
        await session.__aenter__()
        await session.initialize()
        
        self._context_managers.append((stdio_cm, session))
        return session
    
    async def close(self):
        """ Подавляет шумные BaseExceptionGroup / GeneratorExit от anyio/mcp,
        которые возникают при завершении дочерних stdio процессов """
        for stdio_cm, session in self._context_managers:
            try:
                await session.__aexit__(None, None, None)
            except Exception:
                pass  # Игнорируем ошибки закрытия сессии
            try:
                await stdio_cm.__aexit__(None, None, None)
            except Exception:
                pass  # Игнорируем GeneratorExit от anyio

    async def route_query(self, query: str) -> List[str]:
        """ Определяет релевантные плагины, используя все скиллы и примеры запросов """
        
        # Собираем полное описание каждого плагина (все скиллы + примеры)
        plugins_summary = []
        for p in self.structure.plugins:
            skills_desc = "\n    ".join([f"- {s['name']}: {s['description']}" for s in p['skills']])
            plugins_summary.append(f"### {p['name']}\n    {skills_desc}")
        
        prompt = f"""You are a Project Management Router. 
Analyze the user's query and select the 1 to 3 MOST RELEVANT plugin domains.

CRITICAL INSTRUCTION:
- If the query mentions MULTIPLE aspects (e.g., delays AND budget, schedule AND risks), 
  you MUST select ALL relevant plugins, not just one.
- Read skill descriptions carefully. Each description explains what types of queries it handles.
- Match the user's intent to the skills whose descriptions cover the relevant aspects.

Available Domains with Skills:
{chr(10).join(plugins_summary)}

User Query: {query}

Respond with ONLY a valid JSON array of plugin names. 
Example: ["pm-project-core", "pm-value-and-performance"]
Do not add markdown formatting or explanations."""

        loop = asyncio.get_event_loop()
        
        def sync_llm_call():
            return self.llm_client.chat.completions.create(
                model=LLM_MODEL_NAME,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0)
        
        response = await loop.run_in_executor(None, sync_llm_call)
        
        raw_response = response.choices[0].message.content.strip()
        
        # ОТЛАДКА: Показываем сырой ответ LLM
        print(f"{'='*80}")
        print(f"СЫРОЙ ОТВЕТ ОТ LLM:")
        print(f"{'='*80}")
        print(raw_response)
        print(f"{'='*80}\n")
        
        raw_response = raw_response.replace('```json', '').replace('```', '').strip()
        
        try:
            selected_plugins = json.loads(raw_response)
            if isinstance(selected_plugins, str):
                selected_plugins = [selected_plugins]
            
            # Иногда модель может вернуть имя скилла вместо плагина, проверяем
            valid_plugins = []
            for item in selected_plugins:
                if any(plug["name"] == item for plug in self.structure.plugins):
                    valid_plugins.append(item)
                else:
                    # Может это имя скилла? Ищем плагин, которому он принадлежит
                    for plug in self.structure.plugins:
                        if any(skill["name"] == item for skill in plug["skills"]):
                            valid_plugins.append(plug["name"])
                            print(f"LLM вернула имя скилла '{item}', маппим на плагин '{plug['name']}'")
                            break
            
            # Убираем дубликаты
            valid_plugins = list(dict.fromkeys(valid_plugins))
            
            if not valid_plugins:
                valid_plugins = ["pm-project-core"]
                
            print(f"Маршрутизация: выбраны плагины {valid_plugins}")
            return valid_plugins
            
        except json.JSONDecodeError:
            print(f"Ошибка парсинга JSON от роутера. Ответ: {raw_response}. Fallback.")
            return ["pm-project-core"]

    def _build_react_prompt(self, query: str, active_tools: List[Dict[str, Any]], active_plugin_names: List[str]) -> str:
        """ Собирает промпт ReAct только с инструментами и скиллами выбранных плагинов """
        
        skills_context = ""
        for plugin in self.structure.plugins:
            if plugin["name"] in active_plugin_names:
                for skill in plugin["skills"]:
                    skills_context += f"## Skill: {skill['name']} (Plugin: {plugin['name']})\nDescription: {skill['description']}\nInstructions:\n{skill['instructions']}\n\n"
        
        tools_context = []
        for tool in active_tools:
            schema_str = json.dumps(tool.get("inputSchema", {}), indent=2, ensure_ascii=False)
            tools_context.append(f"- {tool['name']}: {tool.get('description', '')}\n  Input schema: {schema_str}")
        
        tool_names = ", ".join([t['name'] for t in active_tools])
        plugins_str = ", ".join(active_plugin_names)
        
        return f"""**CRITICAL WARNING: You are working with REAL data from corporate systems (Jira, SAP, Workday).**
**NEVER invent, modify, or hallucinate any data. Use ONLY the data provided in Observation.**
**If you are unsure, ask for clarification instead of guessing.**
**Respond in the same language as the user's question.**

You are an expert Project Management Assistant (PM IQ), aligned with PMBOK 8 principles.
You are currently operating within the domains: {plugins_str}

You have access to the following skills with detailed instructions:
{skills_context}

You have access to the following tools:
{chr(10).join(tools_context)}

CRITICAL RULES:
1. **DATA INTEGRITY:** 
   - The Observation contains JSON with two fields: 
     * `data` — Markdown table with raw facts (compact format for token efficiency)
     * `widgets` — array of pre-built ECharts chart definitions (JSON)
   - Example structure:
     ```json
     {{
       "data": "| resource_name | allocation_pct | status |\\n|---|---|---|\\n| Alexeev A. | 120 | Overallocated |",
       "widgets": [{{"widget_type": "echarts", "chart_type": "BarChart", ...}}]
     }}
     ```
   - You MUST use ONLY the data from the `data` field. NEVER invent names, numbers, or facts.
   - Parse the Markdown table carefully: each row after the separator represents one record.
   - Example: If `data` contains "Ivanov A.", you MUST write "Ivanov A." — do NOT change it to "Ivan" or invent "Sidorova D.".
   - If `data` contains "Team_1", you MUST write "Team_1" — do NOT invent human names for brigades/teams.

2. **WIDGET RENDERING:**
   - If the Observation contains a `widgets` array with ECharts JSON, you MAY include these widgets in your Final Answer.
   - Each widget in the array has a `description` field explaining when it is useful (from the skill's `available_widgets` declaration).
   - Use these descriptions to decide which widgets are appropriate for the user's request.
   - **CRITICAL: You MUST copy the COMPLETE widget JSON from the Observation, including the full `option` object with `xAxis`, `yAxis`, `series`, etc.**
   - **CONTEXT-AWARE TITLES:** The `title` field in each widget contains only a technical placeholder (e.g., "BarChart", "PieChart", "ActionCard"). You MUST replace it with a meaningful, context-aware title that reflects what the chart shows in the current query context.
     * Example: If the widget has `"title": "BarChart"` and the user asked about "загрузка команды разработки", replace it with `"title": "Загрузка команды разработки"`.
     * Example: If the widget has `"title": "ScatterChart"` and the user asked about risks in "Миграция ERP", replace it with `"title": "Матрица рисков проекта Миграция ERP"`.
     * The title should be descriptive, concise, and include relevant context (project name, team, time period, etc.) from the user's query.
   - Do NOT modify any other fields in the widget JSON (option, series, data, colors, etc.) — only the `title` field should be updated.
   - Do NOT truncate, summarize, or simplify the widget JSON. The UI needs the full structure to render the chart.
   - To include a widget, output it EXACTLY as a ```json ... ``` code block with the updated title.
   - **MULTIPLE WIDGETS:** If the `widgets` array contains multiple items and the user's query covers multiple aspects, include ALL relevant widgets. For example, if the query asks about risks AND mitigation plans, include both the risk visualization widget AND the ActionCard widget.
   - **ACTION CARDS:** If a widget has `widget_type: "action_card"`, it represents an interactive button for the user. ALWAYS include it if the user's query relates to the action it offers (e.g., creating a plan, escalating an issue). For ActionCards, also update the `title` field to be context-aware (e.g., "Критический риск: задержка поставки оборудования").
   - If the user asked for a simple fact, you usually do NOT need widgets.
   - If the user asked for analysis, visualization, or comparison, widgets are RECOMMENDED.

3. **FACT VERIFICATION:**
   - Before providing Final Answer, verify every name and number against the `data` field in Observation.
   - If a name does NOT appear in `data`, you MUST NOT mention it in your answer.
   - Count carefully: if `data` shows 3 resources, say "3 resources", not "4 resources".
   - Check thresholds: if analyzing overload, only resources with allocation > 100% are overloaded.

4. **NEVER HALLUCINATE OBSERVATION:**
   - You MUST call a tool using Action/Action Input format.
   - The system will provide the REAL Observation after the tool is executed.
   - NEVER write "Observation:" in your response — the system provides it automatically.
   - NEVER generate Observation yourself. NEVER invent data that should come from a tool.
   - Wait for the system to provide the real Observation before proceeding to Final Answer.

5. **MULTI-TOOL REQUIREMENT FOR CROSS-DOMAIN QUERIES:**
   - If the user's query mentions MULTIPLE aspects (e.g., delays AND budget, schedule AND risks, cost AND quality), you MUST call tools from ALL relevant domains.
   - Do NOT stop after the first tool call. Continue the Thought/Action/Observation cycle until you have gathered data from all necessary tools.
   - Analyze the query: identify ALL aspects mentioned, then call at least one tool for each aspect.
   - Only provide Final Answer AFTER you have called tools covering all aspects of the query.
   - Example: If the query asks about "delay impact on budget AND risks", you need data from schedule domain, budget/EVM domain, AND risk domain.

6. **MANDATORY USE OF REAL DATA:**
   - When you receive Observation from the system, you MUST use EXACTLY that data.
   - You MUST NOT modify, rename, or "improve" the data in any way.
   - If Observation contains "Ivanov A.", you MUST write "Ivanov A." in your Final Answer.
   - If Observation contains "Team_1", you MUST write "Team_1" — do NOT invent "Dev_Team" or any other name.
   - If Observation contains numbers (e.g., 120%, 80%), you MUST use those exact numbers.
   - VIOLATION OF THIS RULE IS A CRITICAL ERROR.

7. **DEADLOCK ESCAPE:**
   - If you cannot find the required tool in the available tools, DO NOT repeat yourself.
   - Immediately provide a Final Answer explaining what tools are available and suggest the user rephrase the query.
   - Example: "Final Answer: I don't have access to [requested tool]. Available tools in this domain are: [list]. Please rephrase your question."

8. **ERROR HANDLING:**
   - If Observation contains an "error" field (e.g., `{{"error": "Tool not found"}}`), explain the error to the user.
   - Suggest alternative tools or ask the user to rephrase the query.
   - Example: "Final Answer: An error occurred: [error message]. Available alternatives are: [list]. Please try again."

Use the following format strictly:
Question: the input question you must answer
Thought: brief reasoning (1-2 sentences max), then Action
Action: the action to take, MUST be one of [{tool_names}]
Action Input: the input to the action, formatted as a valid JSON string
Observation: the result of the action (this will be provided by the system)
... (this Thought/Action/Action Input/Observation cycle can repeat N times)
Thought: I now know the final answer
Final Answer: the final answer to the original input question (optionally followed by ```json widget blocks if you decided they are useful)

Begin!
Question: {query}"""
    
    def _parse_llm_response(self, text: str) -> Dict[str, str]:
        """ Парсит ответ LLM, извлекая чистые компоненты для санитизации истории """
        
        # Ищем Action. Регулярное выражение захватывает Thought (если есть), Action и Action Input.
        # Оно останавливается перед любым следующим ключевым словом (Observation, Final Answer и т.д)
        action_match = re.search(
            r"(Thought:.*?)?Action:\s*(.*?)\s*Action Input:\s*(.*?)(?=\s*(?:Thought|Observation|Final Answer|Action):|$)",
            text,
            re.IGNORECASE | re.DOTALL)
        
        if action_match:
            thought = action_match.group(1).strip() if action_match.group(1) else ""
            action = action_match.group(2).strip()
            action_input = action_match.group(3).strip()

            if thought.lower().startswith('thought:'):
                thought = thought[8:].strip()
            
            print(f"Найден Action: {action}")
            print(f"Action Input: {action_input[:200]}{'...' if len(action_input) > 200 else ''}")
            
            if re.search(r"Observation:", text, re.IGNORECASE) or re.search(r"Final Answer:", text, re.IGNORECASE):
                print("ВНИМАНИЕ: LLM попыталась сгаллюцинировать Observation/Final Answer. Мы отрежем это из истории.")
            
            return {
                "type": "action",
                "thought": thought,
                "action": action,
                "action_input": action_input}
        
        # Если Action нет, проверяем Final Answer
        final_match = re.search(r"Final Answer:\s*(.*)", text, re.IGNORECASE | re.DOTALL)
        if final_match:
            print(f"Найден Final Answer")
            return {"type": "final_answer", "answer": final_match.group(1).strip()}
        
        # Если ничего не найдено, считаем что LLM просто рассуждает
        print(f"Ничего не найдено, LLM просто рассуждает")
        return {"type": "thought", "text": text}
    
    def _detect_loop(self, history: List[Dict], threshold: int = 3) -> bool:
        """ Обнаруживает зацикливание: если последние N ответов LLM похожи, возвращает True """
        if len(history) < threshold * 2:
            return False
        
        # Берем последние N ответов ассистента
        recent_assistant_msgs = [
            msg["content"] for msg in history[-threshold*2:] 
            if msg["role"] == "assistant"][-threshold:]
        
        # Проверяем, насколько они похожи (простая проверка: первые 100 символов)
        if len(recent_assistant_msgs) < threshold:
            return False
        
        first_msg_prefix = recent_assistant_msgs[0][:100]
        similar_count = sum(1 for msg in recent_assistant_msgs if msg[:100] == first_msg_prefix)
        
        return similar_count >= threshold

    def _detect_hallucinations(self, final_answer: str, observations: List[str]) -> List[str]:
        """ Это только пример! Проверяет, не выдумала ли LLM имена, которых нет в данных """
        # Извлекаем все имена (паттерн: Имя + инициал, например "Alexeev A." или "Иванов И.")
        mentioned_names = set(re.findall(r'\b([А-ЯA-Z][а-яa-z]+\s+[А-ЯA-Z]\.)\b', final_answer))
        
        # Собираем все имена из Observation
        data_names = set()
        for obs in observations:
            data_names.update(re.findall(r'\b([А-ЯA-Z][а-яa-z]+\s+[А-ЯA-Z]\.)\b', obs))
        
        # Находим выдуманные имена
        hallucinated = mentioned_names - data_names
        return list(hallucinated)
    
    async def run(self, query: str, max_iterations: int = 10):
        print(f"\nВопрос: {query}\n" + "-" * 60)
        
        # Мульти-маршрутизация
        try:
            selected_plugins = await self.route_query(query)
        except Exception as e:
            print(f"Ошибка маршрутизации: {e}")
            return "Не удалось определить контекст запроса."
            
        # Агрегация инструментов
        active_tools = [t for t in self.all_tools if t["plugin"] in selected_plugins]
        
        if not active_tools:
            print(f"Для выбранных плагинов не найдено инструментов.")
            return "Не удалось найти подходящие инструменты."

        active_plugin_names = ", ".join(selected_plugins)
        
        # ОТЛАДКА: Показываем, какие инструменты попали в промпт
        print(f"\n{'='*80}")
        print(f"ИНСТРУМЕНТЫ В ПРОМПТЕ REACT:")
        print(f"{'='*80}")
        print(f"Активные плагины: {active_plugin_names}")
        print(f"Количество инструментов: {len(active_tools)}")
        print(f"Список инструментов:")
        for tool in active_tools:
            print(f"  - {tool['name']} (из плагина: {tool['plugin']})")
        print(f"{'='*80}\n")
        
        history = [{"role": "user", "content": self._build_react_prompt(query, active_tools, selected_plugins)}]
        
        for iteration in range(max_iterations):
            print(f"\n[Итерация {iteration + 1}]")
            
            # Детектор зацикливания
            if self._detect_loop(history):
                print("Обнаружено зацикливание LLM. Принудительное завершение.")
                return "Обнаружено зацикливание LLM. Принудительное завершение"
            
            try:
                loop = asyncio.get_event_loop()
                def sync_llm_call():
                    return self.llm_client.chat.completions.create(
                        model=LLM_MODEL_NAME, 
                        messages=history, 
                        temperature=0.1,
                        # TODO: max_tokens
                        max_tokens=4096)
                response = await loop.run_in_executor(None, sync_llm_call)
                llm_text = response.choices[0].message.content
                print(f"LLM (полный ответ):\n{llm_text}\n" + "-" * 30)
            except Exception as e:
                print(f"Ошибка обращения к LLM: {e}")
                return "Ошибка подключения к LLM."
            
            parsed = self._parse_llm_response(llm_text)
            
            # Санизируем историю.
            # Если был вызов Action, мы сохраняем в историю только Thought и Action.
            # Мы намеренно выбрасываем любой галлюцинированный Observation или Final Answer, 
            # который LLM могла сгенерировать в том же ответе, чтобы не "отравлять" контекст для следующего шага
            if parsed["type"] == "action":
                clean_assistant_msg = f"Thought: {parsed.get('thought', '')}\nAction: {parsed['action']}\nAction Input: {parsed['action_input']}"
                history.append({"role": "assistant", "content": clean_assistant_msg})
                
                # ОТЛАДКА: Показываем, что реально сохраняется в историю
                print(f"\n{'='*60}")
                print(f"САНИТИЗИРОВАННОЕ СООБЩЕНИЕ ДЛЯ ИСТОРИИ:")
                print(f"{'='*60}")
                print(clean_assistant_msg)
                print(f"{'='*60}\n")
            else:
                # Для Final Answer или простого Thought сохраняем как есть
                history.append({"role": "assistant", "content": llm_text})
            
            if parsed["type"] == "final_answer":
                # Проверяем на галлюцинации
                observations = [msg["content"] for msg in history if msg["role"] == "user" and msg["content"].startswith("Observation:")]
                hallucinated = self._detect_hallucinations(parsed["answer"], observations)
                
                if hallucinated:
                    print(f"\nВНИМАНИЕ: Обнаружены выдуманные имена: {hallucinated}")
                    print(f"LLM могла сгаллюцинировать данные. Проверьте ответ критически.")
                
                print(f"\nФинальный ответ:\n{parsed['answer']}")
                return parsed["answer"]
            
            elif parsed["type"] == "action":
                # Точное совпадение
                tool = next((t for t in active_tools if t["name"] == parsed["action"]), None)
                # Если не найдено, то ищем похожее имя (fuzzy matching)
                if not tool:
                    from difflib import get_close_matches
                    tool_names = [t["name"] for t in active_tools]
                    matches = get_close_matches(parsed["action"], tool_names, n=1, cutoff=0.7)
                    if matches:
                        tool = next(t for t in active_tools if t["name"] == matches[0])
                        print(f"Автоисправление: '{parsed['action']}' → '{matches[0]}'")
                        parsed["action"] = matches[0]  # исправляем для истории
                if not tool:
                    observation = f"Error: Tool '{parsed['action']}' not found in active tools. Available tools: {', '.join([t['name'] for t in active_tools])}"
                else:
                    try:
                        action_input = json.loads(parsed["action_input"])
                        print(f"Вызов инструмента: {parsed['action']}")
                        print(f"Аргументы: {json.dumps(action_input, ensure_ascii=False)}")
                        
                        session = self.mcp_sessions[tool["server"]]
                        print(f"Вызов MCP-сервера: {tool['server']}...")
                        result = await session.call_tool(parsed["action"], action_input)
                        
                        # Извлекаем текст из результата
                        observation = "\n".join([str(item.text) if hasattr(item, 'text') else str(item) for item in result.content])
                        
                        # ОТЛАДКА: Показываем сырой ответ от MCP
                        print(f"\n{'='*60}")
                        print(f"СЫРОЙ ОТВЕТ ОТ MCP-СЕРВЕРА:")
                        print(observation[:1500] + ("..." if len(observation) > 1500 else ""))
                        print(f"{'='*60}\n")
                        
                    except json.JSONDecodeError:
                        print(f"Ошибка парсинга JSON в Action Input: {parsed['action_input']}")
                        observation = f"Error: Invalid JSON in Action Input."
                    except Exception as e:
                        print(f"Ошибка выполнения инструмента: {e}")
                        import traceback
                        traceback.print_exc()
                        observation = f"Error executing tool: {str(e)}"
                
                # Для консоли обрезаем, но для LLM передаём всё
                display_obs = observation if len(observation) < 600 else observation[:600] + "\n...[обрезано для консоли]..."
                print(f"Observation (для консоли):\n{display_obs}")
                
                # ОТЛАДКА
                print(f"\n{'='*60}")
                print(f"ПЕРЕДАЁТСЯ В LLM КАК OBSERVATION:")
                print(f"Размер: {len(observation)} символов")
                print(f"{'='*60}\n")
                
                # Передаём полный observation в LLM, без обрезки.
                # LLM должна видеть весь JSON виджетов, чтобы воспроизвести его
                history.append({"role": "user", "content": f"Observation: {observation}"})
                
        return "Превышено количество итераций."

async def main():
    agent = PmIqAgent()
    try:
        await agent.initialize()
        
        queries = [
            # "Покажи активные риски с высоким воздействием для проекта 'Миграция ERP' и предложи план реагирования, ответь с диаграммой."
            #"Проанализируй загрузку команды разработки, ответь с диаграммой."
            # # Простой запрос (должен выбрать 1 плагин)
            # "Каков текущий статус критического пути по проекту 'Миграция ERP' и есть ли задержки?",
            # # Кросс-доменный запрос (должен выбрать 2-3 плагина: core + value + risk)
            "Как 5-дневная задержка в проекте 'Миграция ERP' повлияет на бюджет и какие есть связанные с этим риски?",
            # # Запрос к ресурсам
            # "Есть ли перегрузка ресурсов в команде разработки на следующей неделе?"
        ]
        
        for q in queries:
            await agent.run(q)
            print("\n" + "=" * 80 + "\n")
            
    finally:
        print("\nЗавершение работы и очистка ресурсов...")
        await agent.close()

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())