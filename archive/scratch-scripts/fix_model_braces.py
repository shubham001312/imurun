with open('model/fish/src/model.cpp', 'r') as f:
    content = f.read()

# Fix the extra closing braces after the load function
# The pattern is:
#     return m;
# }
# }
# }
# 
# bool LlamaModel::forward

old = '''    return m;
}


}


bool LlamaModel::forward'''

new = '''    return m;
}

bool LlamaModel::forward'''

content = content.replace(old, new)

with open('model/fish/src/model.cpp', 'w') as f:
    f.write(content)

print('Fixed extra braces')