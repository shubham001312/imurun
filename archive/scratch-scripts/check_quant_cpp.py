import re

with open('model/fish/src/quant.cpp', 'r') as f:
    content = f.read()

includes = re.findall(r'#include\s+[<"]([^>"]+)[>"]', content)
print('quant.cpp includes:')
for inc in includes:
    print('  ' + inc)

print('Includes model.h:', 'model.h' in open('model/fish/src/quant.cpp').read())