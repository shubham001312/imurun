with open('model/fish/src/model.cpp', 'r') as f:
    content = f.read()

# Find argmax function
argmax_pos = content.find('int LlamaModel::argmax() const {')
print(f"argmax at position: {argmax_pos}")

# Find the class end (namespace fish closing)
namespace_end = content.find('} // namespace fish')
print(f"namespace end at: {namespace_end}")

# The argmax should be inside the class, but it's currently outside
# We need to move it inside the class definition, before the class closing brace

# Find the class closing brace
class_end = content.rfind('};', 0, namespace_end)
print(f"class end at: {class_end}")

# Show context around class end
print('Context around class end:')
print(repr(content[class_end-100:namespace_end+50]))