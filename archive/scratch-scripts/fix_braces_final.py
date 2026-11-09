with open('model/fish/src/model.cpp', 'r') as f:
    lines = f.readlines()

# Fix: Remove the two extra closing braces after the load function
# Lines 344 and 346 are extra closing braces (1-indexed)
# 0-indexed: lines 343 and 345
# Remove in reverse order to preserve indices
del lines[345]  # Line 346 (0-indexed: 345)
del lines[343]  # Line 344 (index 343)

with open('model/fish/src/model.cpp', 'w') as f:
    f.writelines(lines)

print('Fixed extra closing braces')