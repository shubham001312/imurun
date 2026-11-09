with open('model/fish/src/model.cpp', 'r') as f:
    content = f.read()

# The issue is:
# 1. After forward() there are 3 extra closing braces instead of 1
# 2. argmax is outside the class

# Let's fix the end of the file properly
# The structure should be:
# - forward() function ends with }
# - class LlamaModel ends with }
# - namespace fish ends with }

# Currently there are too many closing braces

# Find the end of forward function
forward_end = content.find('bool LlamaModel::argmax() const {')
if forward_end == -1:
    print("Could not find argmax")
    exit(1)

# The forward function should end with one } then class ends with } then namespace ends with }
# Currently there are too many }

# Let's find the correct end of forward function
# Search backwards from argmax for the end of forward
search_start = content.rfind('return true;\n}', 0, forward_end)
if search_start == -1:
    print("Could not find end of forward")
    exit(1)

print(f"Forward ends around: {search_start}")

# Show context
context = content[search_start:forward_end+50]
print("Context around forward end:")
print(repr(context[:200]))

# The forward function should end with:
#     return true;
# }
# (then class ends, then namespace ends)

# Let's fix by removing extra braces
# The pattern is: return true;\n}\n}\n}\n\nbool LlamaModel::argmax()
# Should be: return true;\n}\n}\n\nint LlamaModel::argmax()

# Find and replace the extra braces
old = '''    return true;
}


}


int LlamaModel::argmax() const {'''

new = '''    return true;
}


int LlamaModel::argmax() const {'''

content = content.replace(old, new)

# Also fix the argmax function - it should use self._cfg or m->cfg_ but it's a member function
# The argmax implementation looks correct, just needs to be properly placed

with open('model/fish/src/model.cpp', 'w') as f:
    f.write(content)

print('Fixed argmax placement')