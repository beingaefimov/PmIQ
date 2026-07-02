# Собирает все файлы по папкам папки в один
# python3 merge.py

import os
with open('all.txt','w',encoding='utf-8') as out:
    for r,d,files in os.walk('.'):
        for f in files:
            p=os.path.join(r,f)
            if os.path.abspath(p)==os.path.abspath('all.txt'): continue
            out.write(f'\n===== {p} =====\n')
            try: out.write(open(p,'r',encoding='utf-8',errors='ignore').read())
            except: pass