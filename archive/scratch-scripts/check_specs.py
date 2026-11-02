import re

with open('model/fish/include/fish/specs.h', 'r') as f:
    content = f.read()

includes = re.findall(r'#include\s+[<"]([^>"]+)[>"]', content)
print('specs.h includes:')
for inc in includes:
    print('  ' + inc)

print('Includes model.h:', 'model.h' in content)