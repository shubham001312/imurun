import re

with open('model/fish/include/fish/model.h', 'r') as f:
    content = f.read()

# Check includes
includes = re.findall(r'#include\s+[<"]([^>"]+)[>"]', content)
print('model.h includes:')
for inc in includes:
    print('  ' + inc)

# Check namespace
idx = content.find('namespace fish')
if idx != -1:
    ns_end = content.find('} // namespace fish', idx)
    class_pos = content.find('class LlamaModel', idx)
    print('namespace starts at:', idx)
    print('class at:', content.find('class LlamaModel'))
    print('namespace ends at:', ns_end)
    print('class in namespace:', class_pos > idx and class_pos < ns_end)