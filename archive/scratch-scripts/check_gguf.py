import re

with open('model/fish/include/fish/gguf.h', 'r') as f:
    content = f.read()

includes = re.findall(r'#include\s+[<"]([^>"]+)[>"]', content)
print('gguf.h includes:')
for inc in includes:
    print('  ' + inc)