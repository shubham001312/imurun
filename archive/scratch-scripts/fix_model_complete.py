with open('model/fish/src/model.cpp', 'r') as f:
    content = f.read()

# Fix 1: Fix the load function - add proper closing brace and return m
# The load function should end with: for(...) m->weights_bytes_ += t.nbytes; return m; }
# Fix 2: Fix the forward function - add proper closing brace
# Fix 3: Fix argmax function - it should be inside the class

# First, let's read the whole file and fix it properly
# The issue is that the load function is missing its closing brace before forward()

# Find the load function end
load_end = content.find('bool LlamaModel::forward(const int32_t* tokens, int n_tok, int pos0)')
if load_end == -1:
    print("Could not find forward function")
    exit(1)

# Find the "return m;" in the load function
load_return = content.rfind('return m;', 0, load_end)
if load_return == -1:
    print("Could not find return m; in load function")
    exit(1)

# The load function should end with "return m;\n}" before the forward function
# Let's find where the load function actually ends (after return m;)
# Look for the pattern: return m;\n}\n\nbool LlamaModel::forward

# Insert missing closing brace if needed
before_forward = content[:load_end]
if not before_forward.rstrip().endswith('}'):
    # Add the missing closing brace before forward
    content = content[:load_end] + '}\n\n' + content[load_end:]
    load_end += 2

# Now fix the forward function - it should end before argmax
forward_end = content.find('int LlamaModel::argmax() const {')
if forward_end == -1:
    print("Could not find argmax function")
    exit(1)

# Check if forward has proper closing brace
forward_content = content[load_end:forward_end]
open_braces = forward_content.count('{')
close_braces = forward_content.count('}')
print(f"Forward function braces: open={open_braces}, close={close_braces}, balance={open_braces - close_braces}")

# If missing closing brace, add it before argmax
if open_braces > close_braces:
    content = content[:forward_end] + '}\n\n' + content[forward_end:]
    forward_end += 2

# Fix argmax - it should be a member function, not a free function
# Find argmax function
argmax_pos = content.find('int LlamaModel::argmax() const {')
if argmax_pos != -1:
    # Check if it's inside the class or outside
    # Look for the class closing brace
    class_end = content.find('}; // namespace fish')
    if class_end != -1 and argmax_pos > class_end:
        # argmax is outside the class - move it inside or fix it
        print("argmax is outside class - needs fixing")

# Check total braces
open_total = content.count('{')
close_total = content.count('}')
print(f"Total braces: open={open_total}, close={close_total}, balance={open_total - close_total}")

with open('model/fish/src/model.cpp', 'w') as f:
    f.write(content)

print('Fixed')