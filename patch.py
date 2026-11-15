# patch_win.py — run once: python patch_win.py
import re, os

def patch(path, old, new, count=1):
    if not os.path.exists(path):
        print(f"[SKIP] {path} not found"); return
    with open(path, 'r', encoding='utf-8') as f:
        c = f.read()
    if new in c:
        print(f"[DONE] {path} already patched"); return
    if old not in c:
        print(f"[FAIL] {path} — pattern not found"); return
    c = c.replace(old, new, count)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(c)
    print(f"[OK]   {path} patched")

# 1) tg_bot.py — fcntl
patch('tg_bot.py',
      'import fcntl\n',
      'try:\n    import fcntl\nexcept ImportError:\n    fcntl = None\n')

# 2) session_cache.py — fcntl
patch('session_cache.py',
      'import fcntl\n',
      'try:\n    import fcntl\nexcept ImportError:\n    fcntl = None\n')

# 3) proxy_manager.py — fcntl
patch('proxy_manager.py',
      'import fcntl\n',
      'try:\n    import fcntl\nexcept ImportError:\n    fcntl = None\n')

# 4) api.py — resource (in multi-import line)
patch('api.py',
      'import asyncio, re, json, random, sys, os, time, resource\n',
      'import asyncio, re, json, random, sys, os, time\n'
      'try:\n    import resource\nexcept ImportError:\n    resource = None\n')

# 5) api.py — guard the setrlimit block that crashes when resource is None
old_block = '''try:
    resource.setrlimit(resource.RLIMIT_NOFILE, (65535, 65535))
except Exception:
    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft < 16384:
        print(f"[api] WARNING: fd limit is {soft} — may exhaust under load", file=sys.stderr)
'''
new_block = '''if resource is not None:
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (65535, 65535))
    except Exception:
        try:
            soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
            if soft < 16384:
                print(f"[api] WARNING: fd limit is {soft} — may exhaust under load", file=sys.stderr)
        except Exception:
            pass
'''
patch('api.py', old_block, new_block)

print("\nDone. Now test with: python -c \"import razorpay; print('ok')\"")