import os

def print_tree(startpath=".", max_depth=3):
    for root, dirs, files in os.walk(startpath):
        depth = root[len(startpath):].count(os.sep)
        if depth >= max_depth:
            continue
        indent = " " * 4 * depth
        print(f"{indent}{os.path.basename(root)}/")
        subindent = " " * 4 * (depth + 1)
        for f in files:
            print(f"{subindent}{f}")

if __name__ == "__main__":
    print_tree(".", max_depth=5)
