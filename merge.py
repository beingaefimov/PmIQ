# Собирает все файлы по папкам в один
# python3 merge.py

import os

# Имена папок, которые нужно полностью пропустить (вместе с их содержимым)
EXCLUDE_DIRS = {
    '.git', '.svn', '.hg',
    'node_modules', '__pycache__', 'venv', '.venv', 'env',
    '.idea', '.vscode', '.vs', 'js',
    'dist', 'build', 'out', 'target', 'bin', 'obj',
}

# Имена файлов, которые нужно пропустить
EXCLUDE_FILES = {
    'all.txt',
    'merge.py',
    'package-lock.json', 'yarn.lock', 'poetry.lock', 'Pipfile.lock',
    '.DS_Store', 'Thumbs.db', 'desktop.ini',
    '.env', '.env.local',
}

# Точные относительные пути, которые нужно исключить
# Например: 'src/config.json' или 'temp/draft.txt'
EXCLUDE_PATHS = {
    # 'some_folder/some_file.txt',
}

with open('all.txt', 'w', encoding='utf-8') as out:
    for r, dirs, files in os.walk('.'):
        # Модифицируем список папок на месте, чтобы os.walk не спускался в исключенные
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        
        for f in files:
            # Пропускаем файлы по имени
            if f in EXCLUDE_FILES:
                continue
                
            p = os.path.join(r, f)
            
            # Пропускаем файлы по точному относительному пути (если заданы)
            rel_path = os.path.relpath(p, '.')
            if rel_path in EXCLUDE_PATHS:
                continue

            out.write(f'\n===== {p} =====\n')
            try:
                # Используем with для корректного закрытия файла после чтения
                with open(p, 'r', encoding='utf-8', errors='ignore') as infile:
                    out.write(infile.read())
            except Exception as e:
                # Если нужно видеть ошибки чтения, раскомментируйте строку ниже:
                # print(f"Не удалось прочитать {p}: {e}")
                pass