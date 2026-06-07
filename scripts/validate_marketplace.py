""" Валидатор структуры маркетплейса PM IQ.
Проверяет целостность marketplace.json, плагинов, скиллов и MCP-конфигурации """

import json
import os
import sys
from pathlib import Path
from typing import List, Tuple

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is not installed. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


class MarketplaceValidator:
    MAX_DESCRIPTION_LENGTH = 1024  # Лимит Copilot CLI

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self.plugins_dir = repo_root / "plugins"
        self.marketplace_file = repo_root / ".github" / "plugin" / "marketplace.json"
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.stats = {
            "plugins_checked": 0,
            "skills_checked": 0,
            "mcp_configs_checked": 0,
        }

    def error(self, msg: str) -> None:
        self.errors.append(msg)
        print(f"❌ ERROR: {msg}", file=sys.stderr)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        print(f"⚠️  WARNING: {msg}")

    def info(self, msg: str) -> None:
        print(f"ℹ️  {msg}")

    def validate(self) -> bool:
        """Запускает все проверки. Возвращает True, если ошибок нет."""
        print("=" * 60)
        print("PM IQ Marketplace Validator")
        print("=" * 60)

        self._validate_marketplace_file()
        self._validate_plugins_consistency()
        self._validate_server_json()

        print("\n" + "=" * 60)
        print(f"Statistics:")
        print(f"  Plugins checked: {self.stats['plugins_checked']}")
        print(f"  Skills checked:  {self.stats['skills_checked']}")
        print(f"  MCP configs:     {self.stats['mcp_configs_checked']}")
        print(f"  Errors:          {len(self.errors)}")
        print(f"  Warnings:        {len(self.warnings)}")
        print("=" * 60)

        self._save_report()
        return len(self.errors) == 0

    def _validate_marketplace_file(self) -> None:
        """Проверяет marketplace.json и все указанные в нём плагины/скиллы."""
        print("\n[1/3] Validating marketplace.json...")

        if not self.marketplace_file.exists():
            self.error(f"Marketplace file not found: {self.marketplace_file}")
            return

        try:
            with open(self.marketplace_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            self.error(f"Invalid JSON in marketplace.json: {e}")
            return

        # Проверяем обязательные поля
        for field in ["name", "plugins"]:
            if field not in data:
                self.error(f"Missing required field '{field}' in marketplace.json")
                return

        plugins = data.get("plugins", [])
        if not plugins:
            self.warn("marketplace.json contains no plugins")

        for plugin in plugins:
            self._validate_plugin_entry(plugin)

    def _validate_plugin_entry(self, plugin: dict) -> None:
        """Валидирует одну запись плагина из marketplace.json."""
        name = plugin.get("name", "<unnamed>")
        self.info(f"Checking plugin: {name}")

        # Обязательные поля плагина
        for field in ["name", "source", "version", "description", "skills"]:
            if field not in plugin:
                self.error(f"Plugin '{name}' missing required field '{field}'")
                return

        source_path = self.repo_root / plugin["source"]
        if not source_path.exists():
            self.error(f"Plugin '{name}': source path does not exist: {source_path}")
            return

        if not source_path.is_dir():
            self.error(f"Plugin '{name}': source path is not a directory: {source_path}")
            return

        # README.md обязателен
        readme = source_path / "README.md"
        if not readme.exists():
            self.error(f"Plugin '{name}': missing README.md at {readme}")

        # .mcp.json опционален, но если есть — проверяем
        mcp_config = source_path / ".mcp.json"
        if mcp_config.exists():
            self.stats["mcp_configs_checked"] += 1
            try:
                with open(mcp_config, "r", encoding="utf-8") as f:
                    json.load(f)
            except json.JSONDecodeError as e:
                self.error(f"Plugin '{name}': invalid JSON in .mcp.json: {e}")

        # Проверяем каждый скилл
        skills = plugin.get("skills", [])
        if not skills:
            self.warn(f"Plugin '{name}': has no skills defined")

        for skill_path_str in skills:
            self._validate_skill_entry(name, skill_path_str)

        self.stats["plugins_checked"] += 1

    def _validate_skill_entry(self, plugin_name: str, skill_path_str: str) -> None:
        """Валидирует один скилл."""
        skill_path = self.repo_root / skill_path_str
        if not skill_path.exists():
            self.error(
                f"Plugin '{plugin_name}': skill path does not exist: {skill_path_str}"
            )
            return

        skill_file = skill_path / "SKILL.md"
        if not skill_file.exists():
            self.error(
                f"Plugin '{plugin_name}': skill missing SKILL.md at {skill_file}"
            )
            return

        # Парсим YAML frontmatter
        try:
            with open(skill_file, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            self.error(f"Plugin '{plugin_name}': cannot read {skill_file}: {e}")
            return

        if not content.startswith("---"):
            self.error(
                f"Plugin '{plugin_name}': SKILL.md at {skill_path_str} "
                "missing YAML frontmatter (must start with ---)"
            )
            return

        # Извлекаем frontmatter
        parts = content.split("---", 2)
        if len(parts) < 3:
            self.error(
                f"Plugin '{plugin_name}': SKILL.md at {skill_path_str} "
                "has malformed YAML frontmatter (missing closing ---)"
            )
            return

        try:
            frontmatter = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError as e:
            self.error(
                f"Plugin '{plugin_name}': invalid YAML in {skill_path_str}: {e}"
            )
            return

        # Проверяем обязательные поля frontmatter
        if "name" not in frontmatter:
            self.error(
                f"Plugin '{plugin_name}': SKILL.md at {skill_path_str} "
                "missing 'name' in frontmatter"
            )
        elif frontmatter["name"] != skill_path.name:
            self.warn(
                f"Plugin '{plugin_name}': skill name '{frontmatter['name']}' "
                f"in frontmatter does not match folder name '{skill_path.name}'"
            )

        if "description" not in frontmatter:
            self.error(
                f"Plugin '{plugin_name}': SKILL.md at {skill_path_str} "
                "missing 'description' in frontmatter"
            )
        else:
            desc = str(frontmatter["description"])
            if len(desc) > self.MAX_DESCRIPTION_LENGTH:
                self.error(
                    f"Plugin '{plugin_name}': skill '{frontmatter.get('name')}' "
                    f"description is {len(desc)} chars (max {self.MAX_DESCRIPTION_LENGTH}). "
                    "Copilot CLI will silently drop this skill!"
                )

        self.stats["skills_checked"] += 1

    def _validate_plugins_consistency(self) -> None:
        """Обратная проверка: все папки в plugins/ должны быть в marketplace.json."""
        print("\n[2/3] Checking plugins consistency (reverse check)...")

        if not self.plugins_dir.exists():
            self.warn(f"Plugins directory does not exist: {self.plugins_dir}")
            return

        if not self.marketplace_file.exists():
            return  # Уже было отмечено как ошибка

        with open(self.marketplace_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        registered_sources = {
            Path(p["source"]).name for p in data.get("plugins", []) if "source" in p
        }
        actual_plugins = {
            d.name
            for d in self.plugins_dir.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        }

        unregistered = actual_plugins - registered_sources
        for plugin_name in sorted(unregistered):
            self.warn(
                f"Plugin folder '{plugin_name}' exists but is NOT registered "
                "in marketplace.json"
            )

        missing = registered_sources - actual_plugins
        for plugin_name in sorted(missing):
            self.error(
                f"Plugin '{plugin_name}' is registered in marketplace.json "
                "but folder does not exist in plugins/"
            )

    def _validate_server_json(self) -> None:
        """Проверяет корневой server.json."""
        print("\n[3/3] Validating server.json...")

        server_file = self.repo_root / "server.json"
        if not server_file.exists():
            self.warn("server.json not found at repository root")
            return

        try:
            with open(server_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            self.error(f"Invalid JSON in server.json: {e}")
            return

        for field in ["name", "version", "description", "packages"]:
            if field not in data:
                self.error(f"server.json missing required field '{field}'")

        # Проверяем packages
        packages = data.get("packages", [])
        if not packages:
            self.warn("server.json has no packages defined")

        for i, pkg in enumerate(packages):
            if "identifier" not in pkg:
                self.error(f"server.json: package #{i} missing 'identifier'")
            if "transport" not in pkg:
                self.error(f"server.json: package #{i} missing 'transport'")

    def _save_report(self) -> None:
        """Сохраняет отчёт о валидации в JSON."""
        report = {
            "status": "success" if not self.errors else "failed",
            "stats": self.stats,
            "errors": self.errors,
            "warnings": self.warnings,
        }
        report_path = self.repo_root / "validation-report.json"
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        self.info(f"Validation report saved to: {report_path}")


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    validator = MarketplaceValidator(repo_root)
    success = validator.validate()

    if success:
        print("\n✅ Marketplace validation PASSED")
        return 0
    else:
        print(f"\n❌ Marketplace validation FAILED ({len(validator.errors)} errors)")
        return 1


if __name__ == "__main__":
    sys.exit(main())
