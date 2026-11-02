with open('model/fish/src/model.cpp', 'r') as f:
    content = f.read()

# The issue is around the end of forward() and argmax
# Current (broken):
#     return true;
# }
# }
# }
# 
# int LlamaModel::argmax() const {
# Should be:
#     return true;
# }
# 
# int LlamaModel::argmax() const {

# Find the exact pattern
old = '''    return true;
}


}


int LlamaModel::argmax() const {'''

new = '''    return true;
}

int LlamaModel::argmax() const {'''

if old in content:
    content = content.replace(old, new)
    print('Fixed argmax braces')
else:
    print('Pattern not found, trying alternative...')
    # Try alternative
    old2 = '''    return true;
}


}


int LlamaModel::argmax() const {'''
    new2 = '''    return true;
}

int LlamaModel::argmax() const {'''
    if old2 in content:
        content = content.replace(old2, new2)
        print('Fixed with alternative pattern')
    else:
        print('Pattern not found')

with open('model/fish/src/model.cpp', 'w') as f:
    f.write(content)

print('Done')