with open('start.py', 'r') as f:
    content = f.read()

content = content.replace(
    'print(f"\\nServer running at http://{host}:{server.port}")',
    'print(f"\\nServer running at http://{cfg.host}:{server.port}")'
)

with open('start.py', 'w') as f:
    f.write(content)

print('Fixed')