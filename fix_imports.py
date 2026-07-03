import os, re

ROOT = "/workspaces/Pdf"
ROOT_PACKAGES = ["config", "services", "providers", "workers", "cache", "api"]

for root, dirs, files in os.walk(ROOT):
    dirs[:] = [d for d in dirs if d not in ['.git', '__pycache__', '.venv', 'node_modules']]
    for fname in files:
        if not fname.endswith(".py"):
            continue
        fpath = os.path.join(root, fname)
        with open(fpath, "r") as f:
            content = f.read()
        original = content
        for pkg in ROOT_PACKAGES:
            content = re.sub(rf'from \.{pkg}\.', f'from {pkg}.', content)
            content = re.sub(rf'import \.{pkg}', f'import {pkg}', content)
        if content != original:
            with open(fpath, "w") as f:
                f.write(content)
            print(f"Tuzatildi: {fpath}")

print("Barcha importlar tuzatildi!")
