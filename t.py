# patch_targets.py — run once: python patch_targets.py
import os

def _append_once(path, marker, block):
    with open(path, 'r', encoding='utf-8') as f:
        c = f.read()
    if marker in c:
        print(f"[SKIP] {path} already patched"); return
    with open(path, 'w', encoding='utf-8') as f:
        f.write(c.rstrip() + '\n' + block)
    print(f"[OK]   {path} patched")

# ── 1) profiles.py — filter PROFILES by supported curl_cffi targets ──
profiles_block = '''

# ── AUTO-FILTER: drop profiles whose target curl_cffi doesn't support ──
def _filter_unsupported_profiles():
    try:
        from curl_cffi.requests import Session as _CS
    except Exception:
        return
    supported = set()
    for p in PROFILES:
        try:
            s = _CS(impersonate=p.impersonate_target)
            s.close()
            supported.add(p.impersonate_target)
        except Exception:
            pass
    if len(supported) < 2:
        return  # detection failed — don't nuke everything
    dropped = [p.name for p in PROFILES if p.impersonate_target not in supported]
    PROFILES[:] = [p for p in PROFILES if p.impersonate_target in supported]
    PROFILES_BY_NAME.clear()
    PROFILES_BY_NAME.update({p.name: p for p in PROFILES})
    if dropped:
        import sys as _sys
        print(f"[profiles] dropped unsupported targets: {dropped}", file=_sys.stderr)

_filter_unsupported_profiles()
'''
_append_once('profiles.py', '_filter_unsupported_profiles', profiles_block)

# ── 2) ja4_engine.py — same filter for _IMPERSONATION_TARGETS ──
ja4_block = '''

# ── AUTO-FILTER: drop impersonation targets curl_cffi doesn't support ──
def _filter_unsupported_targets():
    try:
        from curl_cffi.requests import Session as _CS
    except Exception:
        return
    supported = []
    for t in _IMPERSONATION_TARGETS:
        try:
            s = _CS(impersonate=t)
            s.close()
            supported.append(t)
        except Exception:
            pass
    if len(supported) < 2:
        return
    dropped = [t for t in _IMPERSONATION_TARGETS if t not in supported]
    _IMPERSONATION_TARGETS[:] = supported
    if dropped:
        import sys as _sys
        print(f"[ja4] dropped unsupported targets: {dropped}", file=_sys.stderr)

_filter_unsupported_targets()
'''
_append_once('ja4_engine.py', '_filter_unsupported_targets', ja4_block)

# ── 3) backend_router.py — filter CURL_TARGETS too ──
backend_block = '''

# ── AUTO-FILTER: drop CURL_TARGETS curl_cffi doesn't support ──
def _filter_unsupported_curl_targets():
    try:
        from curl_cffi.requests import Session as _CS
    except Exception:
        return
    supported = []
    for t in CURL_TARGETS:
        try:
            s = _CS(impersonate=t)
            s.close()
            supported.append(t)
        except Exception:
            pass
    if len(supported) < 2:
        return
    CURL_TARGETS[:] = supported

_filter_unsupported_curl_targets()
'''
_append_once('backend_router.py', '_filter_unsupported_curl_targets', backend_block)

print("\nNow verify:")
print("  python -c \"from profiles import PROFILES; print([p.impersonate_target for p in PROFILES])\"")
print("  python -c \"from ja4_engine import _IMPERSONATION_TARGETS; print(_IMPERSONATION_TARGETS)\"")