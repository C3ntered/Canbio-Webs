import sys

with open('Backend/backend.py', 'r') as f:
    lines = f.readlines()

new_lines = []
inserted = False
for line in lines:
    if 'app.mount("/static"' in line and not inserted:
        new_lines.append('@app.get("/bridge.js")\n')
        new_lines.append('async def get_bridge_js():\n')
        new_lines.append('    js_path = os.path.join(frontend_dir, "bridge.js")\n')
        new_lines.append('    if os.path.exists(js_path):\n')
        new_lines.append('        return FileResponse(js_path, media_type="application/javascript")\n')
        new_lines.append('    return {"error": "File not found"}\n\n')
        inserted = True
    new_lines.append(line)

with open('Backend/backend.py', 'w') as f:
    f.writelines(new_lines)
