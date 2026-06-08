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

# Настройки LLM (llama.cpp server)
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
            "path": skill_dir
        }

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
        """ Надежное закрытие ресурсов. 
        Подавляет шумные BaseExceptionGroup / GeneratorExit от anyio/mcp,
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
        """ Определяет 1, 2 или 3 наиболее релевантных плагина 
        для кросс-доменных запросов, экономя токены контекста """
        plugins_summary = "\n".join([
            f"- {p['name']}: {p['skills'][0]['description'] if p['skills'] else 'Инструменты управления проектом'}"
            for p in self.structure.plugins
        ])
        
        prompt = f"""You are a Project Management Router. 
Analyze the user's query and select the 1 to 3 MOST RELEVANT plugin domains needed to answer it comprehensively.

Available Domains:
{plugins_summary}

User Query: {query}

Respond with ONLY a valid JSON array of plugin names. 
Example: ["pm-project-core", "pm-value-and-performance"]
Do not add any markdown formatting (like ```json), explanations, or other text."""

        response = self.llm_client.chat.completions.create(
            model=LLM_MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0 # Детерминированный выбор
        )
        
        raw_response = response.choices[0].message.content.strip()
        
        # Парсинг JSON-массива с защитой от markdown-оберток
        raw_response = raw_response.replace('```json', '').replace('```', '').strip()
        try:
            selected_plugins = json.loads(raw_response)
            if isinstance(selected_plugins, str):
                selected_plugins = [selected_plugins] # Fallback, если LLM вернула строку вместо массива
            
            # Дополнительная валидация: оставляем только те плагины, которые реально существуют
            valid_plugins = [p for p in selected_plugins if any(plug["name"] == p for plug in self.structure.plugins)]
            
            if not valid_plugins:
                valid_plugins = ["pm-project-core"] # Fallback на самый общий плагин
                
            print(f"Маршрутизация: выбраны плагины {valid_plugins}")
            return valid_plugins
            
        except json.JSONDecodeError:
            print(f"Ошибка парсинга JSON от роутера. Ответ: {raw_response}. Используем fallback.")
            return ["pm-project-core"]

    def _build_react_prompt(self, query: str, active_tools: List[Dict[str, Any]], active_plugin_names: List[str]) -> str:
        """Строит промпт ReAct только с инструментами и скиллами выбранных плагинов """
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
        
        return f"""You are an expert Project Management Assistant (PM IQ), aligned with PMBOK 8 principles.
You are currently operating within the domains: {plugins_str}

You have access to the following skills with detailed instructions:
{skills_context}

You have access to the following tools:
{chr(10).join(tools_context)}

CRITICAL RULES:
1. You MUST use ONLY the exact data returned in Observation. 
2. NEVER invent, modify, or "improve" names, numbers, dates, or any facts from the data.
3. If the data contains "Alexeev A.", you MUST write "Alexeev A." - do NOT change it to "Alex" or any other variation.
4. If the data contains "Brigade_1", you MUST write "Brigade_1" - do NOT invent human names.
5. Quote exact values from Observation when presenting findings.

Use the following format strictly:
Question: the input question you must answer
Thought: you should always think about what to do, which skill to follow, and which tool to use
Action: the action to take, MUST be one of [{tool_names}]
Action Input: the input to the action, formatted as a valid JSON string
Observation: the result of the action (this will be provided by the system)
... (this Thought/Action/Action Input/Observation cycle can repeat N times)
Thought: I now know the final answer
Final Answer: the final answer to the original input question

Begin!
Question: {query}
"""
    
    def _parse_llm_response(self, text: str) -> Dict[str, str]:
        """ Парсит ответ LLM с приоритетом Final Answer над Action.
        Также ограничивает Action Input до следующего ключевого слова,
        чтобы корректно парсить многострочный JSON и избегать захвата лишнего текста """
        # Проверяем Final Answer, потому что если он есть, нам не нужны действия
        final_match = re.search(r"Final Answer:\s*(.*)", text, re.IGNORECASE | re.DOTALL)
        if final_match:
            return {"type": "final_answer", "answer": final_match.group(1).strip()}
        # Если Final Answer нет, ищем Action и Action Input
        # Ограничиваем Action Input до следующего ключевого слова или конца текста
        action_match = re.search(
            r"Action:\s*(.*?)\s*Action Input:\s*(.*?)(?=\s*(?:Thought|Observation|Final Answer|Action):|$)",
            text,
            re.IGNORECASE | re.DOTALL
        )
        if action_match:
            return {
                "type": "action",
                "action": action_match.group(1).strip(),
                "action_input": action_match.group(2).strip()
            }
        # Если ничего не найдено, считаем что LLM просто рассуждает
        return {"type": "thought", "text": text}
    
    async def run(self, query: str, max_iterations: int = 5):
        print(f"\nВопрос: {query}\n" + "-" * 60)
        
        # Мульти-маршрутизация
        try:
            selected_plugins = await self.route_query(query)
        except Exception as e:
            print(f"Ошибка маршрутизации: {e}")
            return "Не удалось определить контекст запроса."
            
        # Агрегация инструментов только из выбранных плагинов
        active_tools = [t for t in self.all_tools if t["plugin"] in selected_plugins]
        
        if not active_tools:
            print(f"Для выбранных плагинов не найдено инструментов.")
            return "Не удалось найти подходящие инструменты."

        # ReAct цикл с ограниченным, но кросс-доменным контекстом
        history = [{"role": "user", "content": self._build_react_prompt(query, active_tools, selected_plugins)}]
        
        for iteration in range(max_iterations):
            print(f"\n[Итерация {iteration + 1}]")
            
            try:
                response = self.llm_client.chat.completions.create(
                    model=LLM_MODEL_NAME, messages=history, temperature=0.1, max_tokens=2048
                )
                llm_text = response.choices[0].message.content
                print(f"LLM:\n{llm_text}\n" + "-" * 30)
            except Exception as e:
                print(f"Ошибка обращения к LLM: {e}")
                return "Ошибка подключения к LLM."
            
            parsed = self._parse_llm_response(llm_text)
            history.append({"role": "assistant", "content": llm_text})
            
            if parsed["type"] == "final_answer":
                print(f"\nФинальный ответ:\n{parsed['answer']}")
                return parsed["answer"]
            
            elif parsed["type"] == "action":
                tool = next((t for t in active_tools if t["name"] == parsed["action"]), None)
                if not tool:
                    observation = f"Error: Tool '{parsed['action']}' not found in active tools."
                else:
                    try:
                        action_input = json.loads(parsed["action_input"])
                        print(f"Вызов: {parsed['action']} с аргументами {json.dumps(action_input, ensure_ascii=False)}")
                        session = self.mcp_sessions[tool["server"]]
                        result = await session.call_tool(parsed["action"], action_input)
                        observation = "\n".join([str(item.text) if hasattr(item, 'text') else str(item) for item in result.content])
                    except json.JSONDecodeError:
                        observation = f"Error: Invalid JSON in Action Input."
                    except Exception as e:
                        observation = f"Error executing tool: {str(e)}"
                
                display_obs = observation if len(observation) < 600 else observation[:600] + "\n...[сокращено]..."
                print(f"Observation:\n{display_obs}")
                history.append({"role": "user", "content": f"Observation: {observation}"})
            else:
                pass # LLM просто рассуждает
                
        return "Превышено количество итераций."

async def main():
    agent = PmIqAgent()
    try:
        await agent.initialize()
        
        queries = [
            # Простой запрос (должен выбрать 1 плагин)
            "Каков текущий статус критического пути по проекту 'Миграция ERP' и есть ли задержки?",
            # Кросс-доменный запрос (должен выбрать 2-3 плагина: core + value + risk)
            "Как 5-дневная задержка в проекте 'Миграция ERP' повлияет на бюджет и какие есть связанные с этим риски?",
            # Запрос к ресурсам
            "Есть ли перегрузка ресурсов в команде разработки на следующей неделе?"
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