with open('model/fish/src/model.cpp', 'r') as f:
    content = f.read()

import re
includes = re.findall(r'#include\s+[<\"]([^>\"]+)[>\"]', content)
print('Includes:')
for inc in includes:
    print('  ' + inc)

ns_start = content.find('namespace fish {')
print('namespace fish starts at:', ns_start)

ns_end = content.find('} // namespace fish')
print('namespace fish ends at:', ns_end)

argmax_pos = content.find('int LlamaModel::argmax')
print('argmax at:', argmax_pos)

print('argmax in namespace:', argmax_pos > content.find('namespace fish {') and argmax_pos < content.find('} // namespace fish'))