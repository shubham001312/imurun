import re

with open('model/fish/src/model.cpp', 'r') as f:
    content = f.read()

# Check includes
includes = re.findall(r'#include\s+[<"]([^>"]+)[>"]', content)
print("Includes:")
for inc in includes:
    print("  " + inc)

# Check namespace structure
ns_start = content.find("namespace fish {")
ns_end = content.find("} // namespace fish")
print("Namespace start:", ns_start)
print("Namespace end:", ns_end)

# Check where forward ends
forward_end = content.find("kv_len_ = pos0 + n_tok;    return true;")
print("Forward ends at:", forward_end)

# Show context after forward
ctx = content[content.find("kv_len_ = pos0 + n_tok;    return true;"):content.find("} // namespace fish")]
print("After forward:")
print(ctx[:500])