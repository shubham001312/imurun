import re

with open('model/fish/include/fish/ops.h', 'r') as f:
    content = f.read()

includes = re.findall(r'#include\s+[<"]([^>"]+)[>"]', content)
print('ops.h includes:')
for inc in includes:
    print('  ' + inc)