with open('model/fish/src/model.cpp', 'r') as f:
    content = f.read()

# The exact pattern to fix
old = '''    return true;\n}\n\n}\n\nint LlamaModel::argmax() const {'''

new = '''    return true;\n}\n\nint LlamaModel::argmax() const {'''

if old in content:
    content = content.replace(old, new)
    print('Fixed!')
else:
    print('Pattern not found exactly, trying with slight variations...')
    # Try with different whitespace
    import re
    pattern = r'(\s+return true;\s*\}\s*\n\s*\}\s*\n\s*\}\s*\n\s*int LlamaModel::argmax\(\) const \{)'
    match = re.search(pattern, content)
    if match:
        print(f'Found with regex at {match.start()}')
        content = re.sub(pattern, r'    return true;\n}\n\nint LlamaModel::argmax() const {', content)
        print('Fixed with regex!')
    else:
        print('Still not found')

with open('model/fish/src/model.cpp', 'w') as f:
    f.write(content)

print('Done')