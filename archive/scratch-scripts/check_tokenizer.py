with open('model/fish/include/fish/tokenizer.h', 'r') as f:
    content = f.read()

import re
includes = re.findall(r'#include\s+[<\"]([^>\"]+)[>\"]', content)
print('tokenizer.h includes:')
for inc in includes:
    print('  ' + inc)

print('Includes model.h:', 'model.h' in content)