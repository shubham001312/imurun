import re

with open('model/fish/src/gguf.cpp', 'r') as f:
    content = f.read()

includes = re.findall(r'#include\s+[<"]([^>"]+)[>"]', content)
print('gguf.cpp includes:')
for inc in includes:
    print('  ' + inc)